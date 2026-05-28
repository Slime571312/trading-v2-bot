"""TunerOrchestrator — koordiniert Grid-Search → Proposal-Generierung → Persistenz.

Funktioniert mit oder ohne Anthropic-API-Key:
- MIT Key: Opus 4.7 schreibt einen einzelnen analytischen Vorschlag pro Instrument,
  begründet warum die Top-Combo besser ist
- OHNE Key: Template-Proposal ("Top-Combo hat +0.X R bessere Expectancy")

Threshold für Proposal-Generierung: Score-Delta ≥ 0.10R (0.10 expectancy_r besser).
Kleinere Verbesserungen werden NICHT vorgeschlagen — sonst spamt der Tuner Proposals
für statistisches Rauschen.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from src.chat.state import get_chat_state
from src.config import config
from src.live import get_orchestrator

from .grid_search import GridResult, run_grid_search
from .state import TunerRun, load_history, new_run, save_history

log = logging.getLogger(__name__)

MIN_IMPROVEMENT_THRESHOLD = 0.10  # Score-Delta um einen Proposal zu erzeugen


class TunerOrchestrator:
    """Singleton — hält Lock, History, Status."""

    def __init__(self) -> None:
        self.history = load_history()
        self._lock = asyncio.Lock()
        self._current: TunerRun | None = None

    @property
    def is_running(self) -> bool:
        return self._current is not None and self._current.status == "running"

    @property
    def current_run(self) -> TunerRun | None:
        return self._current

    async def run(
        self,
        *,
        instruments: list[str] | None = None,
        iter_tf: str = "5m",
        bars: int = 1000,
        triggered_by: Literal["manual", "cron"] = "manual",
        use_claude: bool = True,
    ) -> TunerRun:
        """Führt einen Tuner-Lauf durch. Returnt nach Fertigstellung.

        Nimmt internen Lock — kein paralleler Tuner-Run (zu teuer + Capital-Rate-Limit).
        """
        async with self._lock:
            orch = get_orchestrator()
            instruments = instruments or list(orch.state.instruments)
            run = new_run(instruments, iter_tf, bars, triggered_by)
            self._current = run
            self.history.runs.append(run)
            save_history(self.history)

            log.info("TunerRun %s gestartet: %d Instrumente, %d Bars/TF, triggered=%s",
                     run.id, len(instruments), bars, triggered_by)

            try:
                # ── 1. Grid-Search pro Instrument ────────────────────────
                for inst in instruments:
                    log.info("TunerRun %s: Grid-Search %s", run.id, inst)
                    grid = await run_grid_search(
                        instrument=inst,
                        iter_tf=iter_tf,
                        bars=bars,
                        current_rr_threshold=orch.state.rr_threshold,
                        current_sweep_lookback=orch.state.sweep_lookback,
                        risk_pct=orch.state.risk_pct,
                        initial_capital=orch.state.initial_capital,
                    )
                    if grid is None:
                        log.warning("TunerRun %s: %s übersprungen (keine Daten)", run.id, inst)
                        run.results[inst] = {"error": "keine Daten"}
                        continue
                    run.results[inst] = grid.to_json()
                    save_history(self.history)  # incrementally persist

                # ── 2. Proposals erzeugen (über Improvement-Threshold) ───
                proposal_ids = await self._generate_proposals(run, use_claude=use_claude)
                run.proposal_ids = proposal_ids

                # ── 3. Optional Claude-Summary über alle Instrumente ─────
                if use_claude and config.anthropic_api_key and proposal_ids:
                    try:
                        run.claude_summary = await self._claude_summary(run)
                    except Exception as e:
                        log.warning("TunerRun %s: Claude-Summary scheitert: %s", run.id, e)

                run.status = "completed"
                run.finished_at = _now_iso_str()
                log.info("TunerRun %s fertig: %d Proposals erzeugt", run.id, len(proposal_ids))
            except Exception as e:
                run.status = "failed"
                run.error = f"{type(e).__name__}: {e}"
                run.finished_at = _now_iso_str()
                log.exception("TunerRun %s gecrasht", run.id)
            finally:
                save_history(self.history)
                self._current = None

            return run

    async def _generate_proposals(
        self,
        run: TunerRun,
        *,
        use_claude: bool,
    ) -> list[str]:
        """Schaut sich jeden GridResult an und erzeugt Proposal wenn Verbesserung > Threshold.

        Anti-Spam: Es werden nur INSTRUMENT-spezifische Proposals erzeugt — der Bot
        hat aber einen GLOBALEN rr_threshold/sweep_lookback. Lösung: wir gehen über
        alle Instrumente, sammeln die Top-Combo, und schlagen die häufigste/beste
        EINMAL global vor. Das ist eine bewusste Vereinfachung — V2 könnte per-Instrument-
        Configs unterstützen.
        """
        chat_state = get_chat_state()
        candidates: list[dict] = []

        for inst, grid_json in run.results.items():
            if "error" in grid_json or not grid_json.get("best"):
                continue
            best = grid_json["best"]
            improvement = grid_json.get("improvement_vs_current")
            if improvement is None or improvement < MIN_IMPROVEMENT_THRESHOLD:
                continue
            candidates.append({
                "instrument": inst,
                "rr_threshold": best["rr_threshold"],
                "sweep_lookback": best["sweep_lookback"],
                "expectancy_r": best["expectancy_r"],
                "improvement": improvement,
                "n_trades": best["total_trades"],
                "win_rate": best["win_rate"],
            })

        if not candidates:
            log.info("TunerRun %s: keine Combo über MIN_IMPROVEMENT_THRESHOLD (%.2f)",
                     run.id, MIN_IMPROVEMENT_THRESHOLD)
            return []

        # Beste Verbesserung über alle Instrumente nehmen
        candidates.sort(key=lambda c: c["improvement"], reverse=True)
        winner = candidates[0]

        diff = {
            "rr_threshold": winner["rr_threshold"],
            "sweep_lookback": winner["sweep_lookback"],
        }

        if use_claude and config.anthropic_api_key:
            rationale = await self._claude_rationale(winner, candidates, run)
        else:
            rationale = _template_rationale(winner, candidates, run)

        proposal = chat_state.add_proposal(diff, rationale, conversation_id=None)
        log.info("TunerRun %s → Proposal %s: rr=%.1f sl=%d (Δ=%.3fR auf %s)",
                 run.id, proposal.id,
                 winner["rr_threshold"], winner["sweep_lookback"],
                 winner["improvement"], winner["instrument"])
        return [proposal.id]

    async def _claude_rationale(
        self,
        winner: dict,
        candidates: list[dict],
        run: TunerRun,
    ) -> str:
        """Bittet Claude (Opus 4.7) um eine konkrete Begründung für den Proposal."""
        from src.chat.client import get_chat_client

        client = get_chat_client()
        prompt = (
            f"Tuner-Lauf {run.id}: Grid-Search über rr_threshold × sweep_lookback "
            f"auf {len(run.instruments)} Instrumenten ({', '.join(run.instruments)}).\n\n"
            f"Beste Verbesserung gegenüber aktuellen Live-Params:\n"
            f"  Instrument: {winner['instrument']}\n"
            f"  Neue Params: rr_threshold={winner['rr_threshold']}, sweep_lookback={winner['sweep_lookback']}\n"
            f"  Improvement: Δ {winner['improvement']:+.3f} R Expectancy\n"
            f"  Sample-Size: {winner['n_trades']} Trades, Win-Rate {winner['win_rate']:.1%}\n\n"
            f"Alle Kandidaten über Threshold:\n"
            + "\n".join(
                f"  - {c['instrument']}: rr={c['rr_threshold']} sl={c['sweep_lookback']} "
                f"→ {c['expectancy_r']:+.3f}R (Δ {c['improvement']:+.3f}R)"
                for c in candidates[:5]
            )
            + "\n\nSchreibe eine knappe Begründung (max 4 Sätze) für den Proposal — "
            "warum diese Combo besser sein könnte, welche Risiken (z.B. Overfitting bei kleinem N), "
            "ob die Verbesserung statistisch belastbar ist."
        )

        result = await client.run_turn(
            messages=[{"role": "user", "content": prompt}],
            model=config.chat_heavy_model,  # claude-opus-4-7
        )
        return f"[Tuner {run.id}, {config.chat_heavy_model}]\n{result.text}"

    async def _claude_summary(self, run: TunerRun) -> str:
        """Optional: hoch-level Summary über den ganzen Lauf (für UI-Anzeige)."""
        from src.chat.client import get_chat_client

        client = get_chat_client()
        # Bauen wir uns einen kompakten Status-Block
        rows = []
        for inst, grid in run.results.items():
            if "error" in grid:
                rows.append(f"  {inst}: ERROR ({grid['error']})")
                continue
            best = grid.get("best")
            cur_score = grid.get("current_score")
            if best:
                rows.append(
                    f"  {inst}: best rr={best['rr_threshold']} sl={best['sweep_lookback']} "
                    f"→ {best['expectancy_r']:+.3f}R "
                    f"(current {cur_score:+.3f}R, Δ {(best['score']-cur_score) if cur_score is not None else 0:+.3f}R)"
                )

        prompt = (
            f"Tuner-Lauf {run.id} hat folgende Resultate produziert:\n\n"
            + "\n".join(rows) + "\n\n"
            + f"Es wurden {len(run.proposal_ids)} Proposal(s) erzeugt.\n\n"
            "Fasse in 3 Sätzen zusammen: Welche Trends siehst du? Sind die Verbesserungen "
            "groß genug um sie ernst zu nehmen? Was würdest du dem User empfehlen?"
        )
        result = await client.run_turn(
            messages=[{"role": "user", "content": prompt}],
            model=config.chat_heavy_model,
        )
        return result.text


def _template_rationale(winner: dict, candidates: list[dict], run: TunerRun) -> str:
    """Fallback ohne Claude — nur die Zahlen, kein Reasoning."""
    lines = [
        f"[Tuner {run.id}, Template-Fallback — kein ANTHROPIC_API_KEY]",
        "",
        f"Grid-Search auf {len(run.instruments)} Instrumenten "
        f"({', '.join(run.instruments)}, {run.bars_used} bars/TF).",
        "",
        f"Beste Combo: rr_threshold={winner['rr_threshold']}, "
        f"sweep_lookback={winner['sweep_lookback']}",
        f"  → Expectancy {winner['expectancy_r']:+.3f}R "
        f"({winner['n_trades']} Trades, WR {winner['win_rate']:.1%})",
        f"  → Verbesserung gegenüber Live: Δ {winner['improvement']:+.3f}R "
        f"(getestet auf {winner['instrument']})",
        "",
        f"Andere Kandidaten über Threshold ({MIN_IMPROVEMENT_THRESHOLD:+.2f}R):",
    ]
    for c in candidates[:4]:
        if c == winner:
            continue
        lines.append(
            f"  - {c['instrument']}: rr={c['rr_threshold']} sl={c['sweep_lookback']} "
            f"→ Δ {c['improvement']:+.3f}R"
        )
    if winner["n_trades"] < 20:
        lines.append("")
        lines.append("⚠ Achtung: kleine Sample-Size — könnte Overfitting auf den Backtest-Zeitraum sein.")
    return "\n".join(lines)


def _now_iso_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Singleton ───────────────────────────────────────────────────────────


_singleton: TunerOrchestrator | None = None


def get_tuner() -> TunerOrchestrator:
    global _singleton
    if _singleton is None:
        _singleton = TunerOrchestrator()
    return _singleton
