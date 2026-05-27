"""Direkte Engine-Spur für BTC — zeigt jeden Schritt mit Print, deckt RR/TP-Problem auf."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.data.fetcher import load_candles
from src.strategy_core.bias import compute_bias
from src.strategy_core.liquidity import liquidity_levels
from src.strategy_core.pivots import find_pivots
from src.strategy_core.rr import compute_sl, find_tp_target, rr_ratio
from src.strategy_core.sessions import is_in_session
from src.strategy_core.structure import find_bos_after
from src.strategy_core.sweep import detect_sweeps
from src.strategy_core.zones import find_order_block, find_fvgs, compute_equilibrium, in_eq_zone


async def main():
    inst = "BTC"
    bars = {}
    for tf in ["1d", "1h", "30m", "15m", "5m", "1m"]:
        bars[tf] = await load_candles(inst, tf, bars=200)

    bias = compute_bias(bars["1d"])
    print(f"1. Bias: {bias.direction}  last_bos_time={bias.last_bos_time}  level={bias.last_bos_level}")

    sweep_lookback = 50
    htf_used = None
    detected_sweep = None
    for tf in ["1h", "30m", "15m"]:
        df = bars[tf]
        in_sess = is_in_session(df.index[-1], inst)
        levels = liquidity_levels(df, bias.direction)
        sweeps = detect_sweeps(df, levels, tf=tf, lookback=sweep_lookback)
        print(f"   HTF {tf}: in_session={in_sess} levels={len(levels)} sweeps={len(sweeps)}")
        if sweeps and in_sess:
            detected_sweep = sweeps[-1]
            htf_used = tf
            df_htf = df
            print(f"   → use {tf}, latest sweep: {detected_sweep.direction}@{detected_sweep.level} at {detected_sweep.time}")
            break

    if not detected_sweep:
        print("✗ kein Sweep — Setup endet")
        return

    print(f"2. HTF gewählt: {htf_used}")

    for ltf in ["5m", "1m"]:
        df = bars[ltf]
        after = df[df.index >= detected_sweep.time]
        if len(after) < 3:
            continue
        idx_first = df.index.get_loc(after.index[0])
        if isinstance(idx_first, slice):
            idx_first = idx_first.start
        bos = find_bos_after(df, bias.direction, int(idx_first))
        print(f"   LTF {ltf}: bars_after_sweep={len(after)} bos_found={bos is not None}")
        if bos:
            df_ltf = df
            ltf_used = ltf
            sweep_ltf_idx = int(idx_first)
            print(f"   → use {ltf}, BOS @ {bos.body_extreme} broke level {bos.broken_swing.price} at {bos.time}")
            break
    else:
        print("✗ kein LTF-BOS")
        return

    entry_primary = float(df_ltf["close"].iloc[bos.bar_idx])
    sl = compute_sl(bias.direction, detected_sweep, df_htf)
    htf_pivots = find_pivots(df_htf)
    tp = find_tp_target(bias.direction, entry_primary, htf_pivots)
    print(f"3. Entry={entry_primary:.2f} SL={sl:.2f} TP={tp}")
    if tp is None:
        # zeige alle Pivot-Highs/Lows für Debug
        opp_kind = "high" if bias.direction == "short" else "low"
        opp_pivots = [p for p in htf_pivots if p.kind == opp_kind]
        print(f"   ✗ kein TP-Target! Verfügbare {opp_kind}-Pivots: {[(p.price, p.time.isoformat()) for p in opp_pivots[-5:]]}")
        return

    rr = rr_ratio(bias.direction, entry_primary, sl, tp)
    print(f"4. RR_primary = {rr:.3f}  (threshold = 2.0)")

    if rr < 2.0:
        print(f"   ⇒ Retracement-Suche…")
        ob = find_order_block(df_htf, detected_sweep, bos)
        print(f"   OB auf HTF: {ob}")
        if ob:
            new_entry = (ob.upper + ob.lower) / 2
            new_rr = rr_ratio(bias.direction, new_entry, sl, tp)
            print(f"      → entry={new_entry:.2f}  rr={new_rr:.3f}")
        fvgs = find_fvgs(df_ltf, sweep_ltf_idx, bos.bar_idx, bias.direction)
        print(f"   FVGs auf LTF zwischen sweep_idx={sweep_ltf_idx} und bos_idx={bos.bar_idx}: {len(fvgs)}")
        if fvgs:
            f = fvgs[-1]
            new_entry = (f.upper + f.lower) / 2
            new_rr = rr_ratio(bias.direction, new_entry, sl, tp)
            print(f"      → fvg-entry={new_entry:.2f}  rr={new_rr:.3f}")


asyncio.run(main())
