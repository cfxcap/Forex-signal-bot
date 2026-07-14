"""
Forex Signal Bot — end-to-end runner
-------------------------------------
Polls Twelve Data for candle data on your chosen pairs and checks for
FOUR kinds of alerts, each sent to Telegram when triggered:

1. STRUCTURE SIGNAL — BOS + pullback into FVG/order block (15min)
2. CANDLE PATTERN   — pin bar / engulfing reversal (15min, simpler/faster)
3. HTF ZONE TOUCH   — price returning to a 2hr or 4hr supply/demand zone
4. ECONOMIC NEWS    — High/Medium impact news for relevant currencies,
                       15min before release and at release

SETUP
-----
1. Get a free Twelve Data API key: https://twelvedata.com/
2. Create a Telegram bot via @BotFather -> get a bot token
3. Get your chat_id: message your bot once, then visit
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   and read the "chat":{"id": ...} value.
4. Set the three values below (or use environment variables).
5. pip install requests --break-system-packages
6. Run: python3 forex_signal_bot.py

This polls on a timer. For production you'd want to align polls to
candle close times rather than a fixed interval.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone

from smc_detection import check_for_signal, find_order_blocks, check_price_in_zones, OrderBlock
from candle_patterns import check_candle_patterns
from economic_calendar import check_news_alerts

# ---------------------------------------------------------------------
# State files (persisted between runs, important for GitHub Actions
# where each run is a fresh process with no memory)
# ---------------------------------------------------------------------
ALERTED_FILE = os.path.join(os.path.dirname(__file__), "alerted.json")
CANDLE_ALERTED_FILE = os.path.join(os.path.dirname(__file__), "candle_alerted.json")
ZONE_ALERTED_FILE = os.path.join(os.path.dirname(__file__), "zone_alerted.json")
HTF_ZONES_FILE = os.path.join(os.path.dirname(__file__), "htf_zones.json")

# ---------------------------------------------------------------------
# Config — fill these in, or set as environment variables
# ---------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "YOUR_TWELVE_DATA_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "XAU/USD"]  # edit to your majors
INTERVAL = "15min"       # Twelve Data format: 1min,5min,15min,1h,4h,1day...
CANDLE_COUNT = 100       # how many candles to pull per check
POLL_SECONDS = 900       # how often to check (15 min), aligned with candle close.

HTF_TIMEFRAMES = ["2h", "4h"]     # "higher timeframe" zones to watch
HTF_REFRESH_HOURS = 2            # only re-fetch HTF candles this often (rate-limit friendly)
HTF_CANDLE_COUNT = 100

# Detection tuning (see smc_detection.py for what these mean)
SWING_LOOKBACK = 3
FVG_MIN_GAP_PCT = 0.0005
OB_IMPULSE_PCT = 0.003
HTF_OB_IMPULSE_PCT = 0.004   # slightly stricter on higher timeframes


# ---------------------------------------------------------------------
# Generic JSON-set persistence helpers
# ---------------------------------------------------------------------
def load_set(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r") as f:
        return set(tuple(item) for item in json.load(f))


def save_set(path: str, data: set) -> None:
    with open(path, "w") as f:
        json.dump([list(item) for item in data], f)


_already_alerted = load_set(ALERTED_FILE)          # structure signals
_candle_alerted = load_set(CANDLE_ALERTED_FILE)    # candle patterns
_zone_alerted = load_set(ZONE_ALERTED_FILE)         # HTF zone touches


# ---------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------
def fetch_candles(pair: str, interval: str, count: int) -> list:
    """
    Pulls OHLC candles from Twelve Data's time_series endpoint.
    Returns oldest-first list of dicts.
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": count,
        "apikey": TWELVE_DATA_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    if "values" not in data:
        raise RuntimeError(f"Twelve Data error for {pair}: {data}")

    values = list(reversed(data["values"]))  # oldest-first
    return [
        {
            "time": v["datetime"],
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        }
        for v in values
    ]


# ---------------------------------------------------------------------
# Higher timeframe zone caching
# ---------------------------------------------------------------------
def load_htf_zones() -> dict:
    if not os.path.exists(HTF_ZONES_FILE):
        return {"last_refreshed": None, "zones": {}}
    with open(HTF_ZONES_FILE, "r") as f:
        return json.load(f)


def save_htf_zones(state: dict) -> None:
    with open(HTF_ZONES_FILE, "w") as f:
        json.dump(state, f)


def refresh_htf_zones_if_needed(htf_state: dict) -> dict:
    """
    Re-fetches 2h/4h candles and recomputes order block zones, but only
    if HTF_REFRESH_HOURS have passed since the last refresh. This keeps
    API usage well within Twelve Data's free daily limit.
    """
    last_refreshed = htf_state.get("last_refreshed")
    needs_refresh = True
    if last_refreshed:
        elapsed_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last_refreshed)).total_seconds() / 3600
        needs_refresh = elapsed_hours >= HTF_REFRESH_HOURS

    if not needs_refresh:
        return htf_state

    print("[INFO] Refreshing higher-timeframe zones...")
    zones = {}
    for pair in PAIRS:
        zones[pair] = {}
        for tf in HTF_TIMEFRAMES:
            try:
                candles = fetch_candles(pair, tf, HTF_CANDLE_COUNT)
                obs = find_order_blocks(candles, impulse_move_pct=HTF_OB_IMPULSE_PCT)
                zones[pair][tf] = [
                    {"index": ob.index, "top": ob.top, "bottom": ob.bottom, "direction": ob.direction}
                    for ob in obs
                ]
            except Exception as e:
                print(f"[ERROR] Fetching HTF {tf} candles for {pair}: {e}")
                zones[pair][tf] = htf_state.get("zones", {}).get(pair, {}).get(tf, [])

    return {"last_refreshed": datetime.now(timezone.utc).isoformat(), "zones": zones}


# ---------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------
def send_telegram_alert(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[WARN] Telegram send failed: {resp.text}")


def alert(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    send_telegram_alert(full_message)


# ---------------------------------------------------------------------
# Main checks
# ---------------------------------------------------------------------
def check_all_pairs() -> None:
    htf_state = refresh_htf_zones_if_needed(load_htf_zones())

    for pair in PAIRS:
        try:
            candles = fetch_candles(pair, INTERVAL, CANDLE_COUNT)
        except Exception as e:
            print(f"[ERROR] Fetching {pair}: {e}")
            continue

        latest_price = candles[-1]["close"]

        # 1. Structure signal (BOS + FVG/order block pullback)
        signal = check_for_signal(
            pair, candles,
            swing_lookback=SWING_LOOKBACK,
            fvg_min_gap_pct=FVG_MIN_GAP_PCT,
            ob_impulse_pct=OB_IMPULSE_PCT,
        )
        if signal:
            key = (signal.pair, signal.bos_index, signal.zone_type, round(signal.zone_top, 5))
            if key not in _already_alerted:
                _already_alerted.add(key)
                alert(f"[STRUCTURE] {signal.message}")

        # 2. Candle pattern (pin bar / engulfing)
        candle_signal = check_candle_patterns(pair, candles)
        if candle_signal:
            candle_time = candles[-1]["time"]
            key = (pair, candle_signal.pattern, candle_signal.direction, candle_time)
            if key not in _candle_alerted:
                _candle_alerted.add(key)
                alert(f"[CANDLE] {candle_signal.message}")

        # 3. Higher timeframe zone touch
        for tf in HTF_TIMEFRAMES:
            zone_dicts = htf_state["zones"].get(pair, {}).get(tf, [])
            obs = [OrderBlock(**z) for z in zone_dicts]
            hit = check_price_in_zones(latest_price, obs)
            if hit:
                key = (pair, tf, hit.direction, round(hit.top, 5), round(hit.bottom, 5))
                if key not in _zone_alerted:
                    _zone_alerted.add(key)
                    alert(
                        f"[HTF ZONE] {pair}: price at {latest_price:.5f} entered a {tf} "
                        f"{hit.direction} zone ({hit.bottom:.5f}-{hit.top:.5f})"
                    )

    # 4. Economic news (checked once per run, not per pair)
    for news_message in check_news_alerts(PAIRS):
        alert(news_message)

    save_set(ALERTED_FILE, _already_alerted)
    save_set(CANDLE_ALERTED_FILE, _candle_alerted)
    save_set(ZONE_ALERTED_FILE, _zone_alerted)
    save_htf_zones(htf_state)


def run_forever() -> None:
    """For running on your own machine continuously (not used by GitHub Actions)."""
    print(f"Starting forex signal bot — watching {PAIRS} on {INTERVAL} candles.")
    print(f"Polling every {POLL_SECONDS}s. Ctrl+C to stop.\n")
    while True:
        check_all_pairs()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if TWELVE_DATA_API_KEY == "YOUR_TWELVE_DATA_KEY" or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("⚠️  Set your API keys first (see the SETUP comment at the top of this file).")
        print("    You can either edit the constants directly, or set env vars:")
        print("    export TWELVE_DATA_API_KEY=...")
        print("    export TELEGRAM_BOT_TOKEN=...")
        print("    export TELEGRAM_CHAT_ID=...")
    elif os.environ.get("LOOP_MODE") == "1":
        run_forever()
    else:
        check_all_pairs()
