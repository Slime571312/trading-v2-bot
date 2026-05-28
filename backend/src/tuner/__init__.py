"""Self-Improve-Tuner — Nightly Param-Grid-Search + Proposal-Generation.

Spec: Trading/Bot/Lern-Loop.md + Architektur-v2.md (Etappe 7)

Pipeline:
  1. Pro Instrument: Grid-Search über Param-Kombinationen (rr_threshold × sweep_lookback)
  2. Ranking nach expectancy_r (Tie-break: profit_factor)
  3. Vergleich mit aktuell verwendeten Params (aus BotState)
  4. Wenn Top-Combo signifikant besser → Proposal erzeugen
  5. Optional: Claude (Opus 4.7) liefert die Rationale — fallback Template

Läuft per launchd-Plist nächtlich oder manuell via POST /tuner/run.
"""
from .grid_search import GridResult, run_grid_search
from .runner import TunerOrchestrator, get_tuner
from .state import TunerRun, TunerStatus

__all__ = [
    "GridResult", "run_grid_search",
    "TunerRun", "TunerStatus", "TunerOrchestrator", "get_tuner",
]
