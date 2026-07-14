"""
Daily bias / sentiment detection
-----------------------------------
Answers: "what's the overall directional lean on this pair today?"

Logic (based on swing structure on daily candles, same family of
concepts as smc_detection.py):

- Higher highs + higher lows (each more recent swing high/low is above
  the previous one)  -> BULLISH bias
- Lower highs + lower lows -> BEARISH bias
- Anything mixed (e.g. higher high but lower low, or not enough swings
  yet) -> NEUTRAL / ranging, i.e. no clean directional bias

This is intentionally a simple, readable trend read — not a prediction,
just a description of the structure that's currently in place.
"""

from dataclasses import dataclass
from typing import List, Optional, Literal

from smc_detection import find_swing_points, find_bos

Bias = Literal["bullish", "bearish", "neutral"]


@dataclass
class BiasResult:
    pair: str
    bias: Bias
    reason: str


def determine_daily_bias(pair: str, candles: List[dict], swing_lookback: int = 3) -> Optional[BiasResult]:
    swings = find_swing_points(candles, lookback=swing_lookback)

    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    if len(highs) < 2 or len(lows) < 2:
        return BiasResult(
            pair=pair,
            bias="neutral",
            reason="Not enough swing structure yet to call a clear bias.",
        )

    last_high, prev_high = highs[-1], highs[-2]
    last_low, prev_low = lows[-1], lows[-2]

    higher_high = last_high.price > prev_high.price
    higher_low = last_low.price > prev_low.price
    lower_high = last_high.price < prev_high.price
    lower_low = last_low.price < prev_low.price

    # Cross-check with the most recent BOS direction, if any, for extra context
    bos_events = find_bos(candles, swings)
    last_bos_dir = bos_events[-1]["direction"] if bos_events else None

    if higher_high and higher_low:
        bias = "bullish"
        reason = "Structure is making higher highs and higher lows"
    elif lower_high and lower_low:
        bias = "bearish"
        reason = "Structure is making lower highs and lower lows"
    else:
        bias = "neutral"
        reason = "Structure is mixed (no clean higher-high/higher-low or lower-high/lower-low pattern) — likely ranging"

    if last_bos_dir:
        reason += f"; most recent BOS was {last_bos_dir}"

    return BiasResult(pair=pair, bias=bias, reason=reason)


def format_bias_message(result: BiasResult) -> str:
    emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(result.bias, "")
    return f"{emoji} {result.pair} daily bias: {result.bias.upper()} — {result.reason}"


if __name__ == "__main__":
    # Quick sanity check: rising candles should read as bullish
    rising_candles = [
        {"open": 1.10 + i * 0.002, "high": 1.101 + i * 0.002 + (0.003 if i % 4 == 0 else 0),
         "low": 1.099 + i * 0.002 - (0.001 if i % 4 == 2 else 0), "close": 1.1005 + i * 0.002}
        for i in range(30)
    ]
    result = determine_daily_bias("EUR/USD", rising_candles)
    print(format_bias_message(result))
