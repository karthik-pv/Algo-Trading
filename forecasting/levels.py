"""Level map builder: snapshot + daily context -> ranked S/R levels.

Levels (HANDOFF 4):
  - From snapshot: session VWAP, 1m/5m/30m/2H EMA stacks.
  - From D-CSV (daily context): PDH/PDC/PDL/PDOpen, prior-week H/L, D EMAs.
  - Computed: Round100 on BOTH sides of price.
Levels are ranked by proximity to price; the engine attaches store
probabilities (regime-conditioned rejection/hold rates).
"""

from forecasting.regime import _f

ROUND = 100.0


def build_daily_context(daily_rows, today_ist):
    """Daily context from parsed D-CSV rows (list of dicts with date/o/h/l/c/pvt).

    Uses the last COMPLETED daily row (skips today's still-forming row).
    """
    completed = [r for r in daily_rows if r["date"] and r["date"] < today_ist]
    if not completed:
        return None
    prev = completed[-1]
    ctx = {
        "prev_date": prev["date"],
        "pdh": prev.get("high"),
        "pdl": prev.get("low"),
        "pdc": prev.get("close"),
        "pd_open": prev.get("open"),
        "pd_pvt": prev.get("pvt"),
        "d_ema50": prev.get("ema50"),
        "d_ema100": prev.get("ema100"),
        "d_ema200": prev.get("ema200"),
    }
    # Prior-week high/low: previous Mon-Fri window before this week's Monday.
    try:
        from datetime import date as _date, timedelta as _td

        today = _date.fromisoformat(today_ist)
        monday = today - _td(days=today.weekday())
        prev_monday = monday - _td(days=7)
        week_rows = [r for r in completed
                     if prev_monday.isoformat() <= r["date"] < monday.isoformat()]
        if week_rows:
            highs = [r["high"] for r in week_rows if r.get("high") is not None]
            lows = [r["low"] for r in week_rows if r.get("low") is not None]
            if highs:
                ctx["pwh"] = max(highs)
                ctx["pwl"] = min(lows)
    except Exception:
        pass
    return ctx


def round100_sides(price):
    if price is None:
        return None, None
    res = (int(price // ROUND) + 1) * ROUND
    base = int(price // ROUND)
    if float(base) * ROUND == price:
        base -= 1
    sup = base * ROUND
    return res, sup


def collect_levels(data, daily_ctx):
    """Return (price, levels) where levels is a list of
    {family, value, layer} dicts (unsorted, both sides; side decided vs price)."""
    ind = (data or {}).get("INDICATORS") or {}
    m1 = ind.get("1M") or {}
    m5 = ind.get("5M") or {}
    m30 = ind.get("30M") or {}
    h2 = ind.get("2H") or {}

    price = _f(m1.get("CLOSE"))
    if price is None:
        price = _f(m1.get("VWAP"))
    if price is None:
        price = _f(m5.get("CLOSE"))

    levels = []

    def add(family, value, layer="intraday"):
        if value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        levels.append({"family": family, "value": value, "layer": layer})

    # Session VWAP (same session value across TF blocks; prefer 5M).
    add("VWAP", _f(m5.get("VWAP")) or _f(m1.get("VWAP")) or _f(m30.get("VWAP")))

    # EMA stacks from snapshot blocks.
    for fam, block in (("1m", m1), ("5m", m5), ("30m", m30), ("2H", h2)):
        for n in (9, 21, 50, 100, 200):
            add("%s E%d" % (fam, n), _f(block.get("EMA%d" % n)))

    if daily_ctx:
        add("PDH", daily_ctx.get("pdh"), "daily")
        add("PDC", daily_ctx.get("pdc"), "daily")
        add("PDL", daily_ctx.get("pdl"), "daily")
        add("PDOpen", daily_ctx.get("pd_open"), "daily")
        add("PW H", daily_ctx.get("pwh"), "daily")
        add("PW L", daily_ctx.get("pwl"), "daily")
        add("D E50", daily_ctx.get("d_ema50"), "daily")
        add("D E100", daily_ctx.get("d_ema100"), "daily")
        add("D E200", daily_ctx.get("d_ema200"), "daily")

    res100, sup100 = round100_sides(price)
    if res100 is not None:
        levels.append({"family": "Round100", "value": res100, "layer": "computed",
                       "side": "resistance"})
    if sup100 is not None:
        levels.append({"family": "Round100", "value": sup100, "layer": "computed",
                       "side": "support"})

    return price, levels


def rank_levels(price, levels, store):
    """Split levels into resistance (above price) / support (below), sorted by
    distance. Attaches store stats per (family, side, regime) when available.
    Duplicates of the same family+side keep the CLOSEST instance (e.g. EMA
    families appear in multiple TF blocks)."""
    resistance = []
    support = []
    seen = set()
    for lv in sorted(levels, key=lambda x: abs(x["value"] - price) if price else 0):
        if price is not None and lv["value"] > price:
            side = "resistance"
        elif price is not None and lv["value"] < price:
            side = "support"
        else:
            continue
        key = (lv["family"], side)
        if key in seen:
            continue  # keep closest instance only
        seen.add(key)
        entry = dict(lv)
        entry["side"] = side
        entry["distance"] = round(abs(lv["value"] - price), 2) if price is not None else None
        entry["stats"] = store_stats_for(store, lv["family"], side)
        (resistance if side == "resistance" else support).append(entry)
    return resistance, support


def store_stats_for(store, family, side):
    """Lookup placeholder; engine fills regime-conditioned stats."""
    if not store:
        return None
    lv = store.get("levels", {}).get("resistance" if side == "resistance" else "support", {})
    return lv.get(family)
