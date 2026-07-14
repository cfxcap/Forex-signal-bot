"""
Smart Money Concepts (SMC) detection logic sketch
--------------------------------------------------
Detects: swing points -> break of structure (BOS) -> fair value gaps (FVG)
and order blocks (OB) -> combines them into an entry alert.

This is a STARTING POINT, not production code. Every threshold below
(swing_lookback, fvg_min_gap_pct, etc.) is a knob you'll want to tune
against your own charts before trusting any alert.

Candle format expected: list of dicts, oldest first:
{"time": ..., "open": float, "high": float, "low": float, "close": float}
"""

from dataclasses import dataclass
from typing import List, Optional, Literal

Direction = Literal["bullish", "bearish"]


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: Literal["high", "low"]


@dataclass
class FVG:
    start_index: int   # index of the first candle in the 3-candle pattern
    top: float
    bottom: float
    direction: Direction


@dataclass
class OrderBlock:
    index: int          # index of the OB candle itself
    top: float
    bottom: float
    direction: Direction


@dataclass
class Signal:
    pair: str
    direction: Direction
    bos_index: int
    zone_top: float
    zone_bottom: float
    zone_type: Literal["FVG", "OB"]
    message: str


# ---------------------------------------------------------------------
# 1. Swing point detection
# ---------------------------------------------------------------------
def find_swing_points(candles: List[dict], lookback: int = 3) -> List[SwingPoint]:
    """
    A swing high/low is a candle whose high/low is more extreme than
    `lookback` candles on either side. Simple fractal-style detection.
    """
    swings = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window = candles[i - lookback : i + lookback + 1]
        this_high = candles[i]["high"]
        this_low = candles[i]["low"]

        if this_high == max(c["high"] for c in window):
            swings.append(SwingPoint(index=i, price=this_high, kind="high"))
        if this_low == min(c["low"] for c in window):
            swings.append(SwingPoint(index=i, price=this_low, kind="low"))

    return swings


# ---------------------------------------------------------------------
# 2. Break of Structure (BOS) detection
# ---------------------------------------------------------------------
def find_bos(candles: List[dict], swings: List[SwingPoint]) -> List[dict]:
    """
    Walks forward through swing points. A BOS is when a candle CLOSES
    beyond the most recent relevant swing high (bullish BOS) or
    swing low (bearish BOS).

    Returns list of {"index", "direction", "broken_level"}.
    """
    bos_events = []
    last_swing_high = None
    last_swing_low = None

    swing_lookup = {s.index: s for s in swings}

    for i, candle in enumerate(candles):
        if i in swing_lookup:
            s = swing_lookup[i]
            if s.kind == "high":
                last_swing_high = s.price
            else:
                last_swing_low = s.price

        if last_swing_high is not None and candle["close"] > last_swing_high:
            bos_events.append({
                "index": i,
                "direction": "bullish",
                "broken_level": last_swing_high,
            })
            last_swing_high = None  # consumed; wait for next swing

        if last_swing_low is not None and candle["close"] < last_swing_low:
            bos_events.append({
                "index": i,
                "direction": "bearish",
                "broken_level": last_swing_low,
            })
            last_swing_low = None

    return bos_events


# ---------------------------------------------------------------------
# 3. Fair Value Gap (FVG) detection
# ---------------------------------------------------------------------
def find_fvgs(candles: List[dict], min_gap_pct: float = 0.0005) -> List[FVG]:
    """
    3-candle pattern: gap between candle[i-1] and candle[i+1] where
    they don't overlap, leaving an imbalance.

    Bullish FVG: candle[i-1].high < candle[i+1].low
    Bearish FVG: candle[i-1].low  > candle[i+1].high

    min_gap_pct filters out negligible gaps (as a fraction of price,
    e.g. 0.0005 = 5 pips on a pair trading near 1.0000).
    """
    fvgs = []
    for i in range(1, len(candles) - 1):
        prev_c = candles[i - 1]
        next_c = candles[i + 1]
        mid_price = candles[i]["close"]

        if prev_c["high"] < next_c["low"]:
            gap = next_c["low"] - prev_c["high"]
            if gap / mid_price >= min_gap_pct:
                fvgs.append(FVG(
                    start_index=i - 1,
                    top=next_c["low"],
                    bottom=prev_c["high"],
                    direction="bullish",
                ))

        if prev_c["low"] > next_c["high"]:
            gap = prev_c["low"] - next_c["high"]
            if gap / mid_price >= min_gap_pct:
                fvgs.append(FVG(
                    start_index=i - 1,
                    top=prev_c["low"],
                    bottom=next_c["high"],
                    direction="bearish",
                ))

    return fvgs


# ---------------------------------------------------------------------
# 4. Order Block detection
# ---------------------------------------------------------------------
def find_order_blocks(candles: List[dict], impulse_move_pct: float = 0.003) -> List[OrderBlock]:
    """
    Bullish OB: last down-close candle before a strong up-move.
    Bearish OB: last up-close candle before a strong down-move.

    impulse_move_pct defines what counts as a "strong" move
    (e.g. 0.003 = 0.3% move over the next candle).
    """
    obs = []
    for i in range(len(candles) - 1):
        candle = candles[i]
        next_candle = candles[i + 1]
        is_down_candle = candle["close"] < candle["open"]
        is_up_candle = candle["close"] > candle["open"]
        move_pct = abs(next_candle["close"] - candle["close"]) / candle["close"]

        if is_down_candle and next_candle["close"] > candle["close"] and move_pct >= impulse_move_pct:
            obs.append(OrderBlock(
                index=i, top=candle["high"], bottom=candle["low"], direction="bullish",
            ))

        if is_up_candle and next_candle["close"] < candle["close"] and move_pct >= impulse_move_pct:
            obs.append(OrderBlock(
                index=i, top=candle["high"], bottom=candle["low"], direction="bearish",
            ))

    return obs


def check_price_in_zones(price: float, obs: List[OrderBlock], lookback: int = 15) -> Optional[OrderBlock]:
    """
    Checks if `price` currently sits inside any of the most recent
    order blocks (used for higher-timeframe supply/demand zone alerts).

    lookback: only consider the most recent N order blocks, so we don't
    keep re-alerting on zones from weeks ago that price happens to revisit.
    """
    recent_obs = obs[-lookback:] if len(obs) > lookback else obs
    for ob in reversed(recent_obs):  # most recent first
        if ob.bottom <= price <= ob.top:
            return ob
    return None


# ---------------------------------------------------------------------
# 5. Combine into an entry signal
# ---------------------------------------------------------------------
def check_for_signal(
    pair: str,
    candles: List[dict],
    swing_lookback: int = 3,
    fvg_min_gap_pct: float = 0.0005,
    ob_impulse_pct: float = 0.003,
) -> Optional[Signal]:
    """
    Runs the full pipeline on the latest candles and checks whether
    the MOST RECENT candle has pulled back into a valid FVG/OB zone
    following a BOS in the same direction.
    """
    swings = find_swing_points(candles, lookback=swing_lookback)
    bos_events = find_bos(candles, swings)
    if not bos_events:
        return None

    last_bos = bos_events[-1]
    direction = last_bos["direction"]
    bos_index = last_bos["index"]

    fvgs = [f for f in find_fvgs(candles, fvg_min_gap_pct) if f.direction == direction and f.start_index >= bos_index]
    obs = [o for o in find_order_blocks(candles, ob_impulse_pct) if o.direction == direction and o.index >= bos_index]

    latest = candles[-1]
    latest_price = latest["close"]

    for fvg in fvgs:
        if fvg.bottom <= latest_price <= fvg.top:
            return Signal(
                pair=pair,
                direction=direction,
                bos_index=bos_index,
                zone_top=fvg.top,
                zone_bottom=fvg.bottom,
                zone_type="FVG",
                message=f"{pair}: {direction.upper()} BOS + pullback into FVG "
                         f"({fvg.bottom:.5f}-{fvg.top:.5f})",
            )

    for ob in obs:
        if ob.bottom <= latest_price <= ob.top:
            return Signal(
                pair=pair,
                direction=direction,
                bos_index=bos_index,
                zone_top=ob.top,
                zone_bottom=ob.bottom,
                zone_type="OB",
                message=f"{pair}: {direction.upper()} BOS + pullback into order block "
                         f"({ob.bottom:.5f}-{ob.top:.5f})",
            )

    return None


# ---------------------------------------------------------------------
# Example usage (replace with real candles from Twelve Data)
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # Dummy candle data just to show the pipeline runs end-to-end.
    # Real usage: pull candles from Twelve Data's time_series endpoint.
    dummy_candles = [
        {"time": i, "open": 1.1000 + i * 0.0002, "high": 1.1005 + i * 0.0002,
         "low": 1.0995 + i * 0.0002, "close": 1.1003 + i * 0.0002}
        for i in range(50)
    ]

    signal = check_for_signal("EUR/USD", dummy_candles)
    if signal:
        print(signal.message)
    else:
        print("No signal yet.")
