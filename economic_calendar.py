"""
Economic news alerts
----------------------
Pulls this week's economic calendar from a free, publicly accessible
JSON feed (no API key required) and alerts on High/Medium impact events
for currencies relevant to your tracked pairs.

IMPORTANT CAVEAT: this uses an unofficial, undocumented public feed
(commonly used by hobbyist trading tools). It isn't a paid, guaranteed
API — it could change format or go offline without notice. If it stops
working, that's the most likely cause. A more reliable (but paid)
alternative would be a proper economic calendar API (e.g. Trading
Economics, Finnhub's calendar endpoint on a paid plan, etc.).

Timing: since the bot runs on a schedule (every ~15 min), "15 minutes
before" and "at release" are treated as WINDOWS, not exact instants —
an event is caught if it falls within the relevant window at the time
a run happens to execute. Given GitHub Actions schedules can drift or
occasionally skip, don't treat this as split-second precise.
"""

import json
import os
import requests
from datetime import datetime, timedelta, timezone

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_STATE_FILE = os.path.join(os.path.dirname(__file__), "news_alerted.json")

RELEVANT_IMPACTS = {"High", "Medium"}

# Window sizes (minutes). A run happening anywhere inside these windows
# will trigger that stage's alert once.
BEFORE_WINDOW_MINUTES = (5, 40)     # catches "before" alerts even with schedule drift
RELEASE_WINDOW_MINUTES = (-40, 10)  # catches "release" alerts even if the next run is late


def currencies_from_pairs(pairs: list) -> set:
    """
    Extracts the relevant currency codes from pair strings like 'EUR/USD'.
    XAU/USD contributes only USD (gold has no "XAU economic calendar",
    but reacts heavily to USD-moving news).
    """
    currencies = set()
    for pair in pairs:
        for code in pair.split("/"):
            if code != "XAU":
                currencies.add(code)
    return currencies


def load_news_alerted() -> set:
    if not os.path.exists(NEWS_STATE_FILE):
        return set()
    with open(NEWS_STATE_FILE, "r") as f:
        return set(tuple(item) for item in json.load(f))


def save_news_alerted(alerted: set) -> None:
    with open(NEWS_STATE_FILE, "w") as f:
        json.dump([list(item) for item in alerted], f)


def fetch_calendar() -> list:
    resp = requests.get(CALENDAR_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def check_news_alerts(pairs: list) -> list:
    """
    Returns a list of message strings for any news events currently
    inside their "before" or "release" alert window, that haven't
    already been alerted on.
    """
    currencies = currencies_from_pairs(pairs)
    alerted = load_news_alerted()
    messages = []

    try:
        events = fetch_calendar()
    except Exception as e:
        print(f"[ERROR] Fetching economic calendar: {e}")
        return messages

    now = datetime.now(timezone.utc)

    # Diagnostic: show the next few relevant upcoming events every run,
    # so you can confirm the bot is actually seeing real events (even on
    # runs where nothing falls inside an alert window yet).
    upcoming_relevant = []
    for event in events:
        if event.get("country") in currencies and event.get("impact") in RELEVANT_IMPACTS:
            try:
                et = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                if et >= now:
                    upcoming_relevant.append((et, event["country"], event["impact"], event["title"]))
            except (ValueError, KeyError):
                continue
    upcoming_relevant.sort(key=lambda x: x[0])
    if upcoming_relevant:
        print(f"[INFO] Next {min(3, len(upcoming_relevant))} relevant news event(s):")
        for et, cur, imp, title in upcoming_relevant[:3]:
            mins_away = (et - now).total_seconds() / 60
            print(f"       {cur} {imp} — {title} in {mins_away:.0f} min ({et.isoformat()})")
    else:
        print("[INFO] No upcoming High/Medium news events found for your currencies this week.")

    for event in events:
        currency = event.get("country")
        impact = event.get("impact")
        title = event.get("title", "Unknown event")
        event_time_str = event.get("date")

        if currency not in currencies or impact not in RELEVANT_IMPACTS:
            continue
        if not event_time_str:
            continue

        try:
            event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        minutes_until = (event_time - now).total_seconds() / 60

        event_key_base = f"{currency}|{title}|{event_time_str}"

        # "Before" window
        if BEFORE_WINDOW_MINUTES[0] <= minutes_until <= BEFORE_WINDOW_MINUTES[1]:
            key = (event_key_base, "before")
            if key not in alerted:
                alerted.add(key)
                messages.append(
                    f"[NEWS] Upcoming in ~15min: {currency} {impact} impact — {title}"
                )

        # "Release" window
        if RELEASE_WINDOW_MINUTES[0] <= minutes_until <= RELEASE_WINDOW_MINUTES[1]:
            key = (event_key_base, "release")
            if key not in alerted:
                alerted.add(key)
                messages.append(
                    f"[NEWS] Releasing now: {currency} {impact} impact — {title}"
                )

    save_news_alerted(alerted)
    return messages


if __name__ == "__main__":
    print(currencies_from_pairs(["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "XAU/USD"]))
