"""
Forex Signal Bot — end-to-end runner
-------------------------------------
Polls Twelve Data for candle data on your chosen pairs, runs the SMC
detection logic (BOS + FVG/order block pullback), and sends a Telegram
message when a signal fires.

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
candle close times rather than a fixed interval, and add persistent
state so you don't re-alert on the same signal every loop.
"""

import os
import json
import time
import requests
from datetime import datetime

from smc_detection import check_for_signal

# Where alerted-signal history is persisted between runs (important for
# GitHub Actions, where each run is a fresh process with no memory).
ALERTED_FILE = os.path.join(os.path.dirname(__file__), "alerted.json")

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

# Detection tuning (see smc_detection.py for what these mean)
SWING_LOOKBACK = 3
FVG_MIN_GAP_PCT = 0.0005
OB_IMPULSE_PCT = 0.003

# Prevents duplicate alerts for the same BOS/zone across runs.
# Persisted to disk so it survives between separate GitHub Actions runs.
def load_alerted() -> set:
    if not os.path.exists(ALERTED_FILE):
        return set()
    with open(ALERTED_FILE, "r") as f:
        return set(tuple(item) for item in json.load(f))


def save_alerted(alerted: set) -> None:
    with open(ALERTED_FILE, "w") as f:
        json.dump([list(item) for item in alerted], f)


_already_alerted = load_alerted()


# ---------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------
def fetch_candles(pair: str, interval: str, count: int) -> list:
    """
    Pulls OHLC candles from Twelve Data's time_series endpoint.
    Returns oldest-first list of dicts matching smc_detection's format.
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

    # Twelve Data returns newest-first; reverse to oldest-first
    values = list(reversed(data["values"]))
    candles = [
        {
            "time": v["datetime"],
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        }
        for v in values
    ]
    return candles


# ---------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------
def send_telegram_alert(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[WARN] Telegram send failed: {resp.text}")


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def check_all_pairs() -> None:
    for pair in PAIRS:
        try:
            candles = fetch_candles(pair, INTERVAL, CANDLE_COUNT)
        except Exception as e:
            print(f"[ERROR] Fetching {pair}: {e}")
            continue

        signal = check_for_signal(
            pair,
            candles,
            swing_lookback=SWING_LOOKBACK,
            fvg_min_gap_pct=FVG_MIN_GAP_PCT,
            ob_impulse_pct=OB_IMPULSE_PCT,
        )

        if signal is None:
            continue

        # De-dupe: only alert once per (pair, bos_index, zone_type)
        alert_key = (signal.pair, signal.bos_index, signal.zone_type, round(signal.zone_top, 5))
        if alert_key in _already_alerted:
            continue

        _already_alerted.add(alert_key)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        full_message = f"[{timestamp}] {signal.message}"
        print(full_message)
        send_telegram_alert(full_message)

    save_alerted(_already_alerted)


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
        # Local continuous testing: LOOP_MODE=1 python3 forex_signal_bot.py
        run_forever()
    else:
        # Default: single run — this is what GitHub Actions triggers on a schedule.
        check_all_pairs()
