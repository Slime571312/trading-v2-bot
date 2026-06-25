"""Tests für Sortino / Calmar / Omega (Vault/Bot/Performance-Metriken.md)."""
import math

from src.live.perf_metrics import (
    calmar_ratio,
    compute_all,
    max_drawdown,
    omega_ratio,
    sortino_ratio,
)


# ── max_drawdown ─────────────────────────────────────────────────────────


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100, 105, 110, 115]) == 0.0


def test_max_drawdown_simple_dip():
    """Peak 110, Low 90 → -18.18%."""
    eq = [100, 110, 90, 100]
    dd = max_drawdown(eq)
    assert dd < -0.18
    assert dd > -0.19


def test_max_drawdown_short_curve():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100]) == 0.0


# ── sortino_ratio ────────────────────────────────────────────────────────


def test_sortino_returns_none_for_too_few_trades():
    assert sortino_ratio([1.0, 2.0]) is None


def test_sortino_returns_none_no_negative_trades():
    """Ohne Losses → kein std_downside → None."""
    assert sortino_ratio([1.0, 2.0, 3.0]) is None


def test_sortino_positive_for_good_strategy():
    """Mehrheit Gewinne, wenige kleine Verluste → positiver Sortino."""
    rs = [2.0, 1.5, 2.0, -1.0, 2.0, -1.0, 3.0, 2.0]
    s = sortino_ratio(rs)
    assert s is not None
    assert s > 0


def test_sortino_negative_for_bad_strategy():
    """Mehrheit Verluste → negativer Sortino."""
    rs = [-1.0, -1.0, -1.0, 0.5, -1.0, -1.0]
    s = sortino_ratio(rs)
    assert s is not None
    assert s < 0


def test_sortino_higher_for_skewed_winners():
    """Strategie A: symmetrisch; B: gleiche Win-Rate, größere Gewinne
    bei gleichen Verlusten → höherer Sortino (echter Skew-Vorteil).
    Mit |mean_loss|-Fallback wenn alle Losses identisch."""
    sym = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    skewed = [3.0, -1.0, 3.0, -1.0, 3.0, -1.0]
    s_sym = sortino_ratio(sym)
    s_skewed = sortino_ratio(skewed)
    assert s_sym is not None and s_skewed is not None
    assert s_skewed > s_sym


# ── calmar_ratio ─────────────────────────────────────────────────────────


def test_calmar_returns_none_few_trades():
    assert calmar_ratio([1.0, 2.0], [100, 110]) is None


def test_calmar_returns_none_zero_drawdown():
    """Kein DD → division by zero → None."""
    rs = [1.0, 2.0, 1.0]
    assert calmar_ratio(rs, [100, 110, 120, 130]) is None


def test_calmar_positive_with_drawdown():
    rs = [2.0, -1.0, 2.0]
    eq = [100, 120, 110, 130]
    c = calmar_ratio(rs, eq)
    assert c is not None
    assert c > 0


# ── omega_ratio ──────────────────────────────────────────────────────────


def test_omega_returns_none_few_trades():
    assert omega_ratio([1.0, 2.0]) is None


def test_omega_returns_none_no_losses():
    assert omega_ratio([1.0, 2.0, 3.0]) is None


def test_omega_greater_than_one_for_profitable():
    """Mehr Gewinn-Volumen als Verlust-Volumen → Omega > 1."""
    rs = [2.0, -1.0, 2.0, -1.0, 3.0]
    o = omega_ratio(rs)
    assert o is not None
    assert o > 1.0


def test_omega_less_than_one_for_losing():
    rs = [0.5, -1.0, 0.3, -1.5]
    o = omega_ratio(rs)
    assert o is not None
    assert o < 1.0


def test_omega_invariant_under_constant_shift():
    """Wenn alle Returns um +0.5 verschoben werden + threshold auch → Omega gleich."""
    rs = [2.0, -1.0, 2.0, -1.0, 3.0]
    o1 = omega_ratio(rs, threshold=0.0)
    o2 = omega_ratio([r + 0.5 for r in rs], threshold=0.5)
    assert math.isclose(o1, o2, rel_tol=1e-6)


# ── compute_all ──────────────────────────────────────────────────────────


def test_compute_all_returns_full_dict():
    rs = [2.0, -1.0, 2.0, -1.0, 3.0]
    eq = [100, 102, 101, 103, 102, 105]
    out = compute_all(rs, eq)
    assert set(out.keys()) == {"sortino", "calmar", "omega", "max_drawdown_pct"}
    assert out["sortino"] is not None
    assert out["omega"] is not None
    assert out["max_drawdown_pct"] <= 0


def test_compute_all_handles_empty():
    out = compute_all([], [])
    assert out["sortino"] is None
    assert out["calmar"] is None
    assert out["omega"] is None
    assert out["max_drawdown_pct"] == 0.0
