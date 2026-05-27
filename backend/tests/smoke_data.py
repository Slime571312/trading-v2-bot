"""Etappe-1-Smoke-Test — fragt alle 4 Instrumente × 6 TFs = 24 Kombinationen ab.

Läuft direkt gegen Capital (nicht über FastAPI), parallel mit Semaphore=3.
Output zeigt pro Kombination: ✓ + Bar-Count, oder ✗ + Fehlertyp.

Aufruf: backend/.venv/bin/python backend/tests/smoke_data.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Pfad-Setup für Standalone-Ausführung
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config  # noqa: E402
from src.data.fetcher import load_candles  # noqa: E402

INSTRUMENTS = ["DE40", "NASDAQ", "SP500", "BTC"]
TFS = ["1m", "5m", "15m", "30m", "1h", "1d"]


async def one(sem: asyncio.Semaphore, inst: str, tf: str) -> str:
    async with sem:
        t0 = time.perf_counter()
        try:
            df = await load_candles(inst, tf, bars=50, refresh=True)
            dt = (time.perf_counter() - t0) * 1000
            if df.empty:
                return f"⚠ {inst:6s} {tf:4s}  0 bars (Capital lieferte leer)  {dt:6.0f} ms"
            return (
                f"✓ {inst:6s} {tf:4s}  {len(df):3d} bars  "
                f"{df.index[0].strftime('%Y-%m-%d %H:%M')} → "
                f"{df.index[-1].strftime('%Y-%m-%d %H:%M')}  "
                f"close={df.close.iloc[-1]:>10.2f}  {dt:6.0f} ms"
            )
        except Exception as e:
            return f"✗ {inst:6s} {tf:4s}  {type(e).__name__}: {str(e)[:90]}"


async def main() -> int:
    print(f"Capital-Base: {config.capital_base_url}  (demo={config.is_demo})")
    print(f"Broker konfiguriert: {config.broker_configured}\n")
    if not config.broker_configured:
        print("✗ Credentials fehlen in .env — Migration aus v1 läuft via Bash-Snippet.")
        return 1

    sem = asyncio.Semaphore(2)  # Capital rate-limit-freundlich (429-Retry läuft trotzdem)
    tasks = [one(sem, i, t) for i in INSTRUMENTS for t in TFS]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)

    n_ok = sum(1 for r in results if r.startswith("✓"))
    n_warn = sum(1 for r in results if r.startswith("⚠"))
    n_err = sum(1 for r in results if r.startswith("✗"))
    print(f"\n{n_ok}/{len(results)} ok · {n_warn} leer · {n_err} Fehler")
    return 0 if n_err == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
