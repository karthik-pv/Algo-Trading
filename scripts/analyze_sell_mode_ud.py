#!/usr/bin/env python3
"""
One-off analysis (2026-11-03): did Sell Mode U help or hurt vs D?

Reads data/audit/audit_YYYY-MM-DD.json, splits closed LIVE trades by
sell_mode, computes realized P&L per mode, and runs a counterfactual:
for each U/D resting-exit trade, what exit price would the OTHER anchor
have produced, and would it have filled (bounded by the price the trade
actually reached)?

Anchors (PCT comparison mode, current config):
  U anchor = LTP at buy click  -> exit = ltp_click * (1 + target_pct)
  D anchor = average buy price -> exit = buy_avg   * (1 + target_pct)
"""
import glob
import json

TARGET_PCT = {
    "ULTRA_SCALPING": 0.04,
    "SCALPING": 0.06,
    "INTRA": 0.10,
}

def target_pct(strategy, override):
    if override:
        try:
            return float(override) / 100.0
        except (TypeError, ValueError):
            pass
    return TARGET_PCT.get(str(strategy or "").upper(), 0.04)

trades = []
for path in sorted(glob.glob("data/audit/audit_*.json")):
    try:
        rows = json.load(open(path))
    except Exception as e:
        print(f"skip {path}: {e}")
        continue
    for r in rows if isinstance(rows, list) else []:
        if r.get("trade_mode") != "LIVE" or not r.get("buy_success"):
            continue
        sm = r.get("sell_mode")
        if sm not in ("U", "D"):
            continue  # T mode has no resting exit -> not part of U/D question
        trades.append(r)

def pnl(r):
    try:
        lots = float(r.get("lots") or 1)
        return (float(r["sell_avg_price"]) - float(r["buy_avg_price"])) * lots
    except (KeyError, TypeError, ValueError):
        return None

print(f"Closed LIVE U/D trades: {len(trades)}\n")

for mode in ("U", "D"):
    subset = [t for t in trades if t["sell_mode"] == mode]
    wins = [t for t in subset if pnl(t) and pnl(t) > 0]
    losses = [t for t in subset if pnl(t) and pnl(t) <= 0]
    total = sum(pnl(t) or 0 for t in subset)
    print(f"=== Sell Mode {mode}: {len(subset)} trades, {len(wins)} W / {len(losses)} L, total {total:+.1f} pts-lots ===")
    for t in subset:
        p = pnl(t)
        print(f"  {t['date']} {t['symbol']:<22} strat={t['strategy']:<15} "
              f"click={t.get('ltp_click')} avg={t.get('buy_avg_price')} "
              f"exitPx={t.get('resting_exit_price')} soldAvg={t.get('sell_avg_price')} "
              f"src={t.get('sell_trigger_source')} pnl={p if p is None else round(p,1)}")
    print()

# Counterfactual: swap the anchor.
print("=== Counterfactual (opposite anchor's exit price, PCT mode) ===")
for t in trades:
    strat = str(t.get("strategy") or "").upper()
    pct = target_pct(strat, t.get("bp_override_pct") or t.get("target_profit_pct"))
    try:
        click = float(t.get("ltp_click") or 0)
        avg = float(t.get("buy_avg_price") or 0)
        actual_exit = t.get("resting_exit_price")
        actual_exit = float(actual_exit) if actual_exit else None
        sold = float(t.get("sell_avg_price") or 0)
    except (TypeError, ValueError):
        continue
    if not click or not avg:
        continue
    u_exit = click * (1 + pct)
    d_exit = avg * (1 + pct)
    other = d_exit if t["sell_mode"] == "U" else u_exit
    anchor_self = u_exit if t["sell_mode"] == "U" else d_exit
    # Bound on fillability: the market provably reached the actual exit
    # price if the resting exit filled at it.
    filled_at_rest = (t.get("sell_trigger_source") == "RESTING_EXIT")
    # Slippage: click vs fill (positive = bought cheaper than click)
    slip = avg - click
    verdict = ""
    if filled_at_rest and other is not None:
        if t["sell_mode"] == "U":
            # Would D anchor (based on actual avg) have filled?
            verdict = ("D-anchor would also have filled" if d_exit <= actual_exit
                       else "D-anchor higher than price reached - fill unknown")
        else:
            verdict = ("U-anchor would also have filled" if u_exit <= actual_exit
                       else "U-anchor higher than price reached - fill unknown")
    elif t.get("sell_trigger_source") == "MANUAL":
        verdict = "exited manually - resting exit did NOT fill; counterfactual moot"
    print(f"  {t['date']} {t['symbol']:<22} mode={t['sell_mode']} strat={strat:<15} "
          f"ownExit={actual_exit and round(actual_exit,2)} otherAnchorExit={round(other,2)} "
          f"slip={round(slip,2)} | {verdict}")
