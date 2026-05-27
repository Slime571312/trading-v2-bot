"""Lokaler OHLCV-Cache als CSV — pro `(instrument, tf)` eine Datei.

Cache-Frische pro TF aus `config.cache_ttl_seconds`. CSV bewusst statt Parquet
(kein pyarrow-Dependency, debuggbar in jedem Editor, kein Versions-Lock-in).

Datei-Format: Standard-Pandas-CSV mit DatetimeIndex (UTC) + Spalten
`open / high / low / close / volume`.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from src.config import config

log = logging.getLogger(__name__)


def _path(instrument: str, tf: str) -> Path:
    base = Path(config.cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    safe = instrument.replace("/", "_")
    return base / f"{safe}_{tf}.csv"


def is_fresh(instrument: str, tf: str) -> bool:
    """True wenn Cache-Datei jünger als `cache_ttl_seconds[tf]`."""
    p = _path(instrument, tf)
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    ttl = config.cache_ttl_seconds.get(tf, 600)
    return age < ttl


def read(instrument: str, tf: str) -> pd.DataFrame | None:
    """Liefert gecachtes DataFrame oder None falls Datei fehlt / Fehler."""
    p = _path(instrument, tf)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        return df if not df.empty else None
    except Exception as e:
        log.warning("Cache-Read %s/%s fehlgeschlagen: %s", instrument, tf, e)
        return None


def write(instrument: str, tf: str, df: pd.DataFrame) -> None:
    """Atomar via Tempfile + rename, damit ein abgebrochener Schreibvorgang
    den vorhandenen Cache nicht korrumpiert."""
    p = _path(instrument, tf)
    tmp = p.with_suffix(".csv.tmp")
    df.to_csv(tmp)
    tmp.replace(p)
