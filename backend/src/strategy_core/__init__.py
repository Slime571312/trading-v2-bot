"""Strategy-Core — Playbook-Logik, geteilt zwischen Backtest und Live-Bot.

Spec-Quelle: ~/Desktop/tradingbot/Trading/Bot/Playbook.md

Die Module hier sind alle **stateless** — jede Funktion kriegt DataFrames rein
und gibt typed Dataclasses zurück. Kein hidden Side-Effect, kein Module-Level
Caching. Backtest füttert historische Bars, Live füttert Capital-Stream-Bars,
beide rufen dieselben Funktionen.
"""
from ._types import (
    Bias, BiasDir, Equilibrium, FVG, OrderBlock, Side, Signal,
    StructureBreak, Sweep, Swing,
)
from .engine import evaluate

__all__ = [
    "Bias", "BiasDir", "Equilibrium", "FVG", "OrderBlock", "Side",
    "Signal", "StructureBreak", "Sweep", "Swing", "evaluate",
]
