"""
Canonical grid trading bot script (ScriptStrategy).

Upper/lower bounds may be updated at runtime by TradingExecutor via grid_runtime
(adaptive bounds + waterfall protection). Read bounds with ctx.param() each bar.

Hedge mode (P0-1, May 2026):
  * Position state is read from ctx.position.long_size / short_size, which are
    hydrated independently from the qd_strategy_positions table (one row per
    side). This makes "neutral" grids actually neutral — long and short legs
    are tracked separately instead of being netted into a single scalar.
  * Order intent is declared explicitly via ctx.close_short / ctx.open_long /
    ctx.close_long / ctx.open_short instead of ctx.buy / ctx.sell so the
    executor never has to guess whether a buy means "cover the short leg" or
    "stack more longs".
"""
from __future__ import annotations

GRID_BOT_SCRIPT = r'''
def on_init(ctx):
    ctx.param("upperPrice", 0)
    ctx.param("lowerPrice", 0)
    ctx.param("gridCount", 10)
    ctx.param("amountPerGrid", 0)
    ctx.param("gridMode", "arithmetic")
    ctx.param("gridDirection", "neutral")
    ctx.param("adaptiveBounds", True)
    ctx.param("waterfallProtection", True)
    ctx.param("prev_price", 0.0)
    ctx.param("waterfall_pause", False)
    ctx.log("grid bot init")


def _grid_levels(lo, hi, n, mode):
    n = max(2, int(n or 2))
    if str(mode or "").lower() == "geometric" and lo > 0 and hi > lo:
        ratio = (hi / lo) ** (1.0 / (n - 1))
        return [lo * (ratio ** i) for i in range(n)]
    step = (hi - lo) / float(n - 1)
    return [lo + step * i for i in range(n)]


def on_bar(ctx, bar):
    price = float(bar.close or 0)
    if price <= 0:
        return

    if ctx.param("waterfall_pause", False):
        ctx.log("grid paused: waterfall protection")
        return

    upper = float(ctx.param("upperPrice", 0) or 0)
    lower = float(ctx.param("lowerPrice", 0) or 0)
    if upper <= lower:
        return

    grid_count = int(ctx.param("gridCount", 10) or 10)
    amt = float(ctx.param("amountPerGrid", 0) or 0)
    if amt <= 0:
        return

    mode = ctx.param("gridMode", "arithmetic")
    direction = str(ctx.param("gridDirection", "neutral") or "neutral").lower()
    levels = _grid_levels(lower, upper, grid_count, mode)

    prev = float(ctx.param("prev_price", 0) or 0)
    if prev <= 0:
        ctx._params["prev_price"] = price
        return

    # Hedge-mode position view: long_size / short_size are independent legs.
    long_size = float(getattr(ctx.position, "long_size", 0) or 0)
    short_size = float(getattr(ctx.position, "short_size", 0) or 0)

    # Per-bar exposure budget — caps long+short notional so a runaway market
    # can't keep stacking new grid trades forever.
    budget = float(ctx.balance or ctx.equity or 0)
    if budget <= 0:
        budget = amt * grid_count * 2
    base_step = (amt / price) if price > 0 else 0.0

    crossed_down = prev > price
    crossed_up = prev < price

    for lv in levels:
        if prev >= lv > price and crossed_down:
            # Price crossed a grid line going down -> buy.
            if direction in ("long", "neutral"):
                if short_size > 0:
                    use_base = min(base_step, short_size) if base_step > 0 else short_size
                    use_usdt = use_base * price if price > 0 else amt
                    ctx.close_short(amount=use_usdt, price=price, reason="grid_buy_cover")
                    short_size -= use_base
                    leftover_usdt = max(0.0, amt - use_usdt)
                    if leftover_usdt > 0 and (long_size * price + leftover_usdt) <= budget:
                        ctx.open_long(amount=leftover_usdt, price=price, reason="grid_buy_open")
                        long_size += leftover_usdt / price if price > 0 else 0.0
                elif (long_size * price + amt) <= budget:
                    ctx.open_long(amount=amt, price=price, reason="grid_buy_open")
                    long_size += base_step
            elif direction == "short" and short_size > 0:
                use_base = min(base_step, short_size) if base_step > 0 else short_size
                use_usdt = use_base * price if price > 0 else amt
                ctx.close_short(amount=use_usdt, price=price, reason="grid_buy_cover")
                short_size -= use_base
        elif prev <= lv < price and crossed_up:
            # Price crossed a grid line going up -> sell.
            if direction in ("short", "neutral"):
                if long_size > 0:
                    use_base = min(base_step, long_size) if base_step > 0 else long_size
                    use_usdt = use_base * price if price > 0 else amt
                    ctx.close_long(amount=use_usdt, price=price, reason="grid_sell_take")
                    long_size -= use_base
                    leftover_usdt = max(0.0, amt - use_usdt)
                    if leftover_usdt > 0 and (short_size * price + leftover_usdt) <= budget:
                        ctx.open_short(amount=leftover_usdt, price=price, reason="grid_sell_open")
                        short_size += leftover_usdt / price if price > 0 else 0.0
                elif (short_size * price + amt) <= budget:
                    ctx.open_short(amount=amt, price=price, reason="grid_sell_open")
                    short_size += base_step
            elif direction == "long" and long_size > 0:
                use_base = min(base_step, long_size) if base_step > 0 else long_size
                use_usdt = use_base * price if price > 0 else amt
                ctx.close_long(amount=use_usdt, price=price, reason="grid_sell_take")
                long_size -= use_base

    ctx._params["prev_price"] = price
'''


def build_grid_bot_script() -> str:
    return GRID_BOT_SCRIPT.strip() + "\n"
