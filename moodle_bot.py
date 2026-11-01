"""Moodle assignments bot for Telegram.

Runs once per invocation (GitHub Actions calls it on a schedule):
  1. Reads the Moodle calendar export (iCal URL).
  2. Notifies about newly added assignments.
  3. Sends reminders 3 days and 1 day before the deadline.
  4. Answers Telegram messages: any message / "/tasks" -> list of assignments,
     "/done N" -> mark assignment N as submitted (stops its reminders).

State is kept in state.json. Only hashed event IDs are stored there, so the
file reveals nothing about your courses even if the repository is public.

Environment variables:
  MOODLE_ICAL_URL     Moodle calendar export URL
  TELEGRAM_BOT_TOKEN  token from @BotFather
  TELEGRAM_CHAT_ID    your chat id (the bot answers only you)
  DRY_RUN=1           print messages instead of sending (for testing)
  ICAL_FILE           read calendar from a local file instead of the URL
"""

import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
REMINDER_HOURS = [72, 24]  # 3 days, 1 day
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
WEEKDAYS_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


# ---------- iCal ----------

def fetch_calendar() -> str:
    if os.environ.get("ICAL_FILE"):
        return Path(os.environ["ICAL_FILE"]).read_text(encoding="utf-8")
    req = urllib.request.Request(os.environ["MOODLE_ICAL_URL"], headers={"User-Agent": "moodle-bot"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def unescape_ical(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_ical_date(params: str, value: str) -> datetime:
    value = value.strip()
    if "VALUE=DATE" in params and "T" not in value:
        # All-day event: treat as end of that day, local time
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(hour=23, minute=59, tzinfo=LOCAL_TZ)
    fmt = "%Y%m%dT%H%M%S"
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], fmt).replace(tzinfo=timezone.utc)
    tzid = re.search(r"TZID=([^;:]+)", params)
    tz = LOCAL_TZ
    if tzid:
        try:
            tz = ZoneInfo(tzid.group(1).strip('"'))
        except Exception:
            pass
    return datetime.strptime(value, fmt).replace(tzinfo=tz)


def parse_events(text: str) -> list[dict]:
    # Unfold continuation lines (lines starting with space/tab continue the previous one)
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur and cur.get("uid") and cur.get("due"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, value = line.split(":", 1)
            name, _, params = key.partition(";")
            name = name.upper()
            if name == "UID":
                cur["uid"] = value.strip()
            elif name == "SUMMARY":
                cur["title"] = unescape_ical(value).strip()
            elif name == "CATEGORIES":
                cur["course"] = unescape_ical(value).strip()
            elif name == "DTSTART":
                cur["due"] = parse_ical_date(params, value)
    for e in events:
        e["id"] = hashlib.sha256(e["uid"].encode()).hexdigest()[:16]
        e.setdefault("title", "(ללא שם)")
        e.setdefault("course", "")
    return events


# ---------- formatting ----------

def fmt_due(due: datetime) -> str:
    d = due.astimezone(LOCAL_TZ)
    return f"יום {WEEKDAYS_HE[d.weekday()]} {d:%d/%m} בשעה {d:%H:%M}"


def fmt_left(due: datetime, now: datetime) -> str:
    secs = (due - now).total_seconds()
    if secs <= 0:
        return "עבר המועד"
    hours = int(secs // 3600)
    if hours < 1:
        return "פחות משעה"
    if hours < 48:
        return f"עוד {hours} שעות"
    return f"עוד {hours // 24} ימים"


def fmt_event(e: dict, now: datetime) -> str:
    course = f"\n📚 {html.escape(e['course'])}" if e["course"] else ""
    return (f"<b>{html.escape(e['title'])}</b>{course}\n"
            f"🗓 {fmt_due(e['due'])} ({fmt_left(e['due'], now)})")


def upcoming(events: list[dict], state: dict, now: datetime) -> list[dict]:
    done = set(state["done"])
    return sorted((e for e in events if e["due"] > now and e["id"] not in done),
                  key=lambda e: e["due"])


def task_list(events: list[dict], state: dict, now: datetime) -> str:
    items = upcoming(events, state, now)
    if not items:
        return "🎉 אין מטלות פתוחות כרגע!"
    parts = [f"📋 <b>יש לך {len(items)} מטלות פתוחות:</b>"]
    for i, e in enumerate(items, 1):
        urgent = "🔴 " if e["due"] - now <= timedelta(hours=72) else ""
        parts.append(f"{i}. {urgent}{fmt_event(e, now)}")
    parts.append("<i>הגשת? שלח /done ומספר המטלה (למשל /done 1) כדי להפסיק תזכורות עליה.</i>")
    return "\n\n".join(parts)


HELP = ("🤖 <b>בוט המטלות מהמודל</b>\n\n"
        "שלח לי כל הודעה (או /tasks) ותקבל את רשימת המטלות הפתוחות.\n"
        "/done 2 – סימון מטלה מספר 2 כמוגשת (לא אזכיר עליה יותר)\n"
        "/undo – ביטול הסימון האחרון\n\n"
        "אני מודיע לך על כל מטלה חדשה ומזכיר 3 ימים ויום לפני ההגשה.")


# ---------- Telegram ----------

def tg(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    with urllib.request.urlopen(url, data=data, timeout=60) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result}")
    return result["result"]


def send(text: str):
    if DRY_RUN:
        print("----- message -----\n" + text)
        return
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    # Telegram limit is 4096 chars per message; split on blank lines if needed
    chunk = ""
    for block in text.split("\n\n"):
        if len(chunk) + len(block) + 2 > 4000:
            tg("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML")
            chunk = ""
        chunk = f"{chunk}\n\n{block}" if chunk else block
    if chunk:
        tg("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML")


def handle_messages(events: list[dict], state: dict, now: datetime):
    if DRY_RUN:
        return
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
    updates = tg("getUpdates", offset=state["tg_offset"], timeout=0)
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat_id:
            continue  # ignore anyone who isn't you
        text = (msg.get("text") or "").strip()
        reply_to_command(text, events, state, now)


def reply_to_command(text: str, events: list[dict], state: dict, now: datetime):
    cmd = text.split()[0].lower() if text else ""
    if cmd in ("/help", "/start"):
        send(HELP)
    elif cmd == "/done":
        m = re.search(r"\d+", text)
        items = upcoming(events, state, now)
        if not m or not 1 <= int(m.group()) <= len(items):
            send("לא הבנתי איזו מטלה. שלח /tasks לרשימה, ואז למשל /done 1")
            return
        e = items[int(m.group()) - 1]
        state["done"].append(e["id"])
        send(f"✅ סימנתי כמוגשת: <b>{html.escape(e['title'])}</b>\n(טעית? שלח /undo)")
    elif cmd == "/undo":
        if not state["done"]:
            send("אין מה לבטל.")
            return
        undone = state["done"].pop()
        title = next((e["title"] for e in events if e["id"] == undone), "המטלה")
        send(f"↩️ החזרתי לרשימה: <b>{html.escape(title)}</b>")
    else:
        send(task_list(events, state, now))


# ---------- main logic ----------

def load_state() -> dict | None:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("done", [])
        state.setdefault("tg_offset", 0)
        return state
    return None


def process(events: list[dict], state: dict | None, now: datetime) -> dict:
    first_run = state is None
    if first_run:
        state = {"seen": {}, "done": [], "tg_offset": 0}
    seen = state["seen"]  # id -> list of reminder thresholds already sent

    for e in sorted(events, key=lambda e: e["due"]):
        hours_left = (e["due"] - now).total_seconds() / 3600
        if e["id"] not in seen:
            # Thresholds already passed at discovery time are covered by this message
            seen[e["id"]] = [h for h in REMINDER_HOURS if hours_left <= h]
            if not first_run and hours_left > 0:
                send(f"🆕 <b>מטלה חדשה במודל!</b>\n\n{fmt_event(e, now)}")
            continue
        if e["id"] in state["done"] or hours_left <= 0:
            continue
        due_now = [h for h in REMINDER_HOURS if hours_left <= h and h not in seen[e["id"]]]
        if due_now:
            seen[e["id"]] = sorted(set(seen[e["id"]]) | {h for h in REMINDER_HOURS if hours_left <= h})
            send(f"⏰ <b>תזכורת: מתקרבת הגשה!</b>\n\n{fmt_event(e, now)}")

    if first_run:
        send("✅ הבוט מחובר למודל!\n\n" + HELP + "\n\n" + task_list(events, state, now))

    # Forget events that ended over two weeks ago so the state file stays small
    alive = {e["id"] for e in events if e["due"] > now - timedelta(days=14)}
    state["seen"] = {k: v for k, v in seen.items() if k in alive}
    state["done"] = [d for d in state["done"] if d in alive]
    return state


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    try:
        events = parse_events(fetch_calendar())
    except Exception as exc:
        print(f"Could not read Moodle calendar: {exc}", file=sys.stderr)
        sys.exit(1)
    state = process(events, state, now)
    handle_messages(events, state, now)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
