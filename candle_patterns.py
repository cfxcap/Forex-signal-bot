"""
Simple candle reversal pattern detection
------------------------------------------
Two classic, easy-to-read reversal signals:

1. Pin bar (hammer / shooting star): small body, one long wick at
   least 2x the body size, positioned toward one end of the range.
   - Long lower wick + small body near the top -> bullish pin bar
   - Long upper wick + small body near the bottom -> bearish pin bar

2. Engulfing: the latest candle's body completely covers the
   previous candle's body, in the opposite direction.
   - Bearish candle followed by a larger bullish candle -> bullish engulfing
   - Bullish candle followed by a larger bearish candle -> bearish engulfing

These are intentionally simple pattern-matching rules, not the BOS/FVG/OB
logic in smc_detection.py. Treat this as a lighter-weight, higher-frequency
signal, not a substitute for the structural one.
"""

from dataclasses import dataclass
from typing import List, Optional, Literal

Direction = Literal["bullish", "bearish"]


@dataclass
class CandleSignal:
    pair: str
    pattern: Literal["pin_bar", "engulfing"]
    direction: Direction
    message: str


def _body(candle: dict) -> float:
    return abs(candle["close"] - candle["open"])


def _range(candle: dict) -> float:
    return candle["high"] - candle["low"]


def detect_pin_bar(candle: dict, wick_ratio: float = 2.0, max_body_pct: float = 0.35) -> Optional[Direction]:
    """
    wick_ratio: how many times longer the dominant wick must be than the body.
    max_body_pct: body must be no more than this fraction of the full range.
    """
    total_range = _range(candle)
    if total_range == 0:
        return None

    body = _body(candle)
    if body / total_range > max_body_pct:
        return None  # body too large to count as a pin bar

    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]

    if body == 0:
        body = total_range * 0.01  # avoid divide-by-zero on doji-like candles

    if lower_wick / body >= wick_ratio and lower_wick > upper_wick:
        return "bullish"   # long lower wick = rejection of lower prices
    if upper_wick / body >= wick_ratio and upper_wick > lower_wick:
        return "bearish"   # long upper wick = rejection of higher prices

    return None


def detect_engulfing(prev_candle: dict, candle: dict) -> Optional[Direction]:
    prev_bullish = prev_candle["close"] > prev_candle["open"]
    prev_bearish = prev_candle["close"] < prev_candle["open"]
    this_bullish = candle["close"] > candle["open"]
    this_bearish = candle["close"] < candle["open"]

    prev_top = max(prev_candle["open"], prev_candle["close"])
    prev_bottom = min(prev_candle["open"], prev_candle["close"])
    this_top = max(candle["open"], candle["close"])
    this_bottom = min(candle["open"], candle["close"])

    engulfs = this_top >= prev_top and this_bottom <= prev_bottom

    if prev_bearish and this_bullish and engulfs:
        return "bullish"
    if prev_bullish and this_bearish and engulfs:
        return "bearish"

    return None


def check_candle_patterns(pair: str, candles: List[dict]) -> Optional[CandleSignal]:
    """
    Checks only the MOST RECENT candle for a pattern (this keeps alerts
    tied to "right now" rather than re-flagging old candles every run).
    """
    if len(candles) < 2:
        return None

    latest = candles[-1]
    prev = candles[-2]

    engulfing_dir = detect_engulfing(prev, latest)
    if engulfing_dir:
        return CandleSignal(
            pair=pair,
            pattern="engulfing",
            direction=engulfing_dir,
            message=f"{pair}: {engulfing_dir.upper()} engulfing candle on 15min",
        )

    pin_dir = detect_pin_bar(latest)
    if pin_dir:
        return CandleSignal(
            pair=pair,
            pattern="pin_bar",
            direction=pin_dir,
            message=f"{pair}: {pin_dir.upper()} pin bar on 15min",
        )

    return None


if __name__ == "__main__":
    # Quick sanity check with dummy candles
    bullish_engulf_candles = [
        {"open": 1.1050, "high": 1.1055, "low": 1.1030, "close": 1.1035},  # bearish
        {"open": 1.1030, "high": 1.1070, "low": 1.1025, "close": 1.1065},  # bullish, engulfs
    ]
    print(check_candle_patterns("EUR/USD", bullish_engulf_candles))

    pin_bar_candles = [
        {"open": 1.1000, "high": 1.1010, "low": 1.0995, "close": 1.1005},
        {"open": 1.1005, "high": 1.1012, "low": 1.0950, "close": 1.1008},  # long lower wick
    ]
    print(check_candle_patterns("GBP/USD", pin_bar_candles))
