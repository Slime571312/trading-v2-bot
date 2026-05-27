"""Zentrale Bot-Config — Pydantic-Settings, liest .env + Defaults.

Spec-Quelle: ~/Desktop/tradingbot/Trading/Bot/Playbook.md + Architektur-v2.md.

Variablen-Namen 1:1 wie v1 (~/trading-ui/.env.local), damit die Migration aus
v1 trivial ist und beide Bot-Welten gegen dasselbe Demo-Konto fahren.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Broker ──────────────────────────────────────────────────────────
    capital_base_url: str = Field(
        default="https://demo-api-capital.backend-capital.com/api/v1",
        description="Demo: demo-api-capital..., Live: api-capital...",
    )
    capital_api_key: str = Field(default="", description="Capital.com API-Key")
    capital_email: str = Field(default="", description="Capital.com Login-Email")
    capital_api_password: str = Field(default="", description="Capital.com API-Passwort (NICHT das Login-Passwort)")

    # ─── Chat ────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API-Key für Chat-Bot")
    chat_default_model: str = Field(default="claude-sonnet-4-6", description="Default-Modell für Chat")
    chat_heavy_model: str = Field(default="claude-opus-4-7", description="Modell für schweres Reasoning")

    # ─── Backend ─────────────────────────────────────────────────────────
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)
    log_level: Literal["debug", "info", "warning", "error"] = Field(default="info")

    # ─── Strategie — Playbook-Parameter ──────────────────────────────────
    # Default-Werte nach ~/Desktop/tradingbot/Trading/Bot/Playbook.md
    instruments: list[str] = Field(
        default_factory=lambda: ["DE40", "NASDAQ", "SP500", "BTC"],
        description="Initial-Scope der vier Instrumente (Playbook-Namen, nicht Capital-Epics)",
    )
    htf_liquidity: list[str] = Field(
        default_factory=lambda: ["1h", "30m", "15m"],
        description="TFs auf denen LQ gemarkiert wird — höchster gewinnt",
    )
    ltf_trigger: list[str] = Field(
        default_factory=lambda: ["5m", "1m"],
        description="TFs auf denen LTF-BOS gesucht wird",
    )
    bias_source_tf: str = Field(default="1d", description="TF aus dem der Daily-BoS gelesen wird")
    rr_threshold_direct: float = Field(
        default=2.0,
        description="Direct-Entry erst ab RR ≥ dieser Schwelle, sonst Retracement",
    )

    # ─── Sessions (deutsche Zeit, vgl. Playbook) ─────────────────────────
    london_open_start: str = Field(default="09:00")
    london_open_end: str = Field(default="12:00")
    ny_open_start: str = Field(default="15:30")
    ny_open_end: str = Field(default="18:00")
    crypto_247: bool = Field(default=True, description="BTC tradet 24/7, Indizes nur Session")

    # ─── Risk ────────────────────────────────────────────────────────────
    risk_pct_per_trade: float = Field(default=0.01, description="1 % des Equity pro Trade")
    max_trades_per_day: int = Field(default=3)
    max_daily_loss_pct: float = Field(default=0.03, description="3 % Daily-Loss-Stopp")

    # ─── Data-Cache ──────────────────────────────────────────────────────
    cache_dir: str = Field(default="backend/data/cache", description="Pfad für OHLCV-CSV-Cache")
    cache_ttl_seconds: dict[str, int] = Field(
        default_factory=lambda: {
            "1m": 60, "5m": 300, "15m": 600,
            "30m": 900, "1h": 1800, "1d": 86400,
        },
        description="Cache-Frische pro TF (kürzer = öfter refreshen)",
    )

    @property
    def is_demo(self) -> bool:
        return "demo" in self.capital_base_url.lower()

    @property
    def broker_configured(self) -> bool:
        return bool(self.capital_api_key and self.capital_email and self.capital_api_password)


config = BotConfig()


# ─── Instrument-Mapping: Playbook-Name → Capital-Epic ────────────────────
# Aus v1 (~/trading-ui/src/app/api/markets/route.ts) übernommen
EPIC_MAP: dict[str, str] = {
    "DE40": "DE40",
    "NASDAQ": "US100",
    "SP500": "US500",
    "BTC": "BTCUSD",
}

# ─── TF-Mapping: Playbook-String → Capital-Resolution ────────────────────
# Capital-Werte aus der v1-fetchCandles-Logik abgeleitet
RESOLUTION_MAP: dict[str, str] = {
    "1m": "MINUTE",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "30m": "MINUTE_30",
    "1h": "HOUR",
    "4h": "HOUR_4",
    "1d": "DAY",
    "1w": "WEEK",
}


def to_epic(instrument: str) -> str:
    """Playbook-Name → Capital-Epic. Wirft KeyError wenn unbekannt."""
    if instrument not in EPIC_MAP:
        raise KeyError(f"Unbekanntes Instrument '{instrument}'. Bekannt: {list(EPIC_MAP)}")
    return EPIC_MAP[instrument]


def to_resolution(tf: str) -> str:
    """Playbook-TF-String → Capital-Resolution. Wirft KeyError wenn unbekannt."""
    if tf not in RESOLUTION_MAP:
        raise KeyError(f"Unbekannter TF '{tf}'. Bekannt: {list(RESOLUTION_MAP)}")
    return RESOLUTION_MAP[tf]
