"""Moodle assignments bot for Telegram.

Runs once per invocation (GitHub Actions calls it on a schedule):
  1. Reads the Moodle calendar export (iCal URL).
  2. Notifies about newly added assignments and about deadline changes.
  3. Sends reminders 3 days, 1 day and 3 hours before the deadline.
  4. Keeps quiet at night (23:00-07:00): messages wait for the morning,
     unless the deadline itself falls before the morning.
  5. Sends a weekly digest on Sunday morning.
  6. Alerts in Telegram when the Moodle calendar can't be read for a while.
  7. Answers Telegram messages: any message / "/tasks" -> list of assignments,
     a "✅" button (or "/done N") marks an assignment as submitted.

State is kept in state.json. Only hashed event IDs and timestamps are stored
there, so the file reveals nothing about your courses even if the repository
is public.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
REMINDER_HOURS = [72, 24, 3]       # 3 days, 1 day, 3 hours
QUIET_START, QUIET_END = 23, 7     # no messages 23:00-07:00 (local time)
DIGEST_WEEKDAY = 6                 # Sunday (Monday is 0)
FAIL_ALERT_AFTER = timedelta(minutes=30)
KEEPALIVE_DAYS = 25                # GitHub disables schedules after 60 idle days
DUE_TOLERANCE = 60                 # seconds; smaller due-date shifts are ignored
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
WEEKDAYS_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


# ---------- iCal ----------

def fetch_calendar() -> str:
    if os.environ.get("ICAL_FILE"):
        return Path(os.environ["ICAL_FILE"]).read_text(encoding="utf-8")
    req = urllib.request.Request(os.environ["MOODLE_ICAL_URL"], headers={
        # Some university firewalls reject requests that don't look like a browser
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept": "text/calendar,*/*",
    })
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
    if "BEGIN:VCALENDAR" not in text:
        # An expired or wrong link usually returns an HTML page instead of a calendar
        raise ValueError("the response is not an iCal calendar (expired link?)")
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
            elif name == "URL":
                cur["url"] = value.strip()
    for e in events:
        e["id"] = hashlib.sha256(e["uid"].encode()).hexdigest()[:16]
        e.setdefault("title", "(ללא שם)")
        e.setdefault("course", "")
    return events


def event_link(e: dict) -> str | None:
    """Direct link if the calendar has one, else that day in the Moodle calendar."""
    if e.get("url", "").startswith(("https://", "http://")):
        return e["url"]
    parts = urllib.parse.urlsplit(os.environ.get("MOODLE_ICAL_URL", ""))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    # The export URL is <moodle root>/calendar/export_execute.php; keep the root's path
    root = parts.path.split("/calendar/")[0] if "/calendar/" in parts.path else ""
    return (f"{parts.scheme}://{parts.netloc}{root}/calendar/view.php"
            f"?view=day&time={int(e['due'].timestamp())}")


# ---------- time helpers ----------

def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(LOCAL_TZ).hour
    return hour >= QUIET_START or hour < QUIET_END


def quiet_end(now: datetime) -> datetime:
    """The next 07:00 local time (the end of the current or next quiet period)."""
    local = now.astimezone(LOCAL_TZ)
    end = local.replace(hour=QUIET_END, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_END:
        end = (end + timedelta(days=1)).replace(hour=QUIET_END)
    return end


def can_send_about(e: dict, now: datetime) -> bool:
    # At night only if waiting for the morning would be too late
    return not is_quiet(now) or e["due"] <= quiet_end(now)


def from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


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
    url = event_link(e)
    link = f'\n🔗 <a href="{html.escape(url, quote=True)}">פתח במודל</a>' if url else ""
    return (f"<b>{html.escape(e['title'])}</b>{course}\n"
            f"🗓 {fmt_due(e['due'])} ({fmt_left(e['due'], now)}){link}")


def done_button(e: dict) -> list:
    return [[{"text": "✅ הגשתי", "callback_data": f"done:{e['id']}"}]]


def numbered_buttons(items: list[dict]) -> list:
    buttons = [{"text": f"✅ {i}", "callback_data": f"done:{e['id']}"}
               for i, e in enumerate(items, 1)]
    return [buttons[i:i + 5] for i in range(0, len(buttons), 5)]


def upcoming(events: list[dict], state: dict, now: datetime) -> list[dict]:
    done = set(state["done"])
    return sorted((e for e in events if e["due"] > now and e["id"] not in done),
                  key=lambda e: e["due"])


def numbered(items: list[dict], now: datetime) -> list[str]:
    parts = []
    for i, e in enumerate(items, 1):
        urgent = "🔴 " if e["due"] - now <= timedelta(hours=72) else ""
        parts.append(f"{i}. {urgent}{fmt_event(e, now)}")
    return parts


DONE_HINT = "<i>הגשת? לחץ למטה על המספר של המטלה, ולא אזכיר עליה יותר.</i>"


def task_list(events: list[dict], state: dict, now: datetime) -> tuple[str, list | None]:
    items = upcoming(events, state, now)
    if not items:
        return "🎉 אין מטלות פתוחות כרגע!", None
    parts = [f"📋 <b>יש לך {len(items)} מטלות פתוחות:</b>", *numbered(items, now), DONE_HINT]
    return "\n\n".join(parts), numbered_buttons(items)


def weekly_digest(events: list[dict], state: dict, now: datetime) -> tuple[str, list | None]:
    items = [e for e in upcoming(events, state, now) if e["due"] <= now + timedelta(days=7)]
    if not items:
        return "☀️ <b>בוקר טוב!</b> אין הגשות השבוע 🎉", None
    parts = [f"☀️ <b>בוקר טוב! השבוע מוגשות {len(items)} מטלות:</b>",
             *numbered(items, now), DONE_HINT]
    return "\n\n".join(parts), numbered_buttons(items)


HELP = ("🤖 <b>בוט המטלות מהמודל</b>\n\n"
        "שלח לי כל הודעה (או /tasks) ותקבל את רשימת המטלות הפתוחות.\n"
        "הגשת? לחץ על הכפתור ✅ שמתחת להודעה, ולא אזכיר על המטלה יותר.\n"
        "/undo – ביטול הסימון האחרון\n\n"
        "אני מודיע על כל מטלה חדשה ועל כל שינוי במועד הגשה, מזכיר 3 ימים, "
        "יום ו־3 שעות לפני ההגשה, ושולח סיכום שבועי ביום ראשון בבוקר. "
        "בין 23:00 ל־07:00 אני שקט, אלא אם ההגשה עצמה לפני הבוקר.\n\n"
        "<i>אני בודק הודעות כל כמה דקות, אז התשובה לוקחת קצת זמן.</i>")


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


def send(text: str, buttons: list | None = None):
    # Telegram limit is 4096 chars per message; split on blank lines if needed
    chunks, chunk = [], ""
    for block in text.split("\n\n"):
        if chunk and len(chunk) + len(block) + 2 > 4000:
            chunks.append(chunk)
            chunk = ""
        chunk = f"{chunk}\n\n{block}" if chunk else block
    if chunk:
        chunks.append(chunk)
    for i, part in enumerate(chunks):
        markup = buttons if buttons and i == len(chunks) - 1 else None
        if DRY_RUN:
            print("----- message -----\n" + part)
            if markup:
                print("[buttons] " + " | ".join(b["text"] for row in markup for b in row))
            continue
        params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": part,
                  "parse_mode": "HTML",
                  "link_preview_options": json.dumps({"is_disabled": True})}
        if markup:
            params["reply_markup"] = json.dumps({"inline_keyboard": markup})
        tg("sendMessage", **params)


def handle_messages(events: list[dict], state: dict, now: datetime):
    if DRY_RUN:
        return
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
    updates = tg("getUpdates", offset=state["tg_offset"], timeout=0,
                 allowed_updates=json.dumps(["message", "callback_query"]))
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        if "callback_query" in u:
            cq = u["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != chat_id:
                continue  # ignore anyone who isn't you
            reply_to_button(cq, events, state)
            continue
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat_id:
            continue  # ignore anyone who isn't you
        text = (msg.get("text") or "").strip()
        reply_to_command(text, events, state, now)


def answer_button(cq: dict, text: str):
    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=text)
    except Exception as exc:
        # Telegram refuses answers to old button presses; the chat reply is enough
        print(f"answerCallbackQuery: {exc}", file=sys.stderr)


def reply_to_button(cq: dict, events: list[dict], state: dict):
    action, _, event_id = (cq.get("data") or "").partition(":")
    event = next((e for e in events if e["id"] == event_id), None)
    if action == "done":
        if event is None:
            answer_button(cq, "המטלה כבר לא ברשימה")
        elif event_id in state["done"]:
            answer_button(cq, "כבר סימנת אותה")
        else:
            state["done"].append(event_id)
            answer_button(cq, "סומן ✅")
            send(f"✅ סימנתי כמוגשת: <b>{html.escape(event['title'])}</b>",
                 [[{"text": "↩️ ביטול", "callback_data": f"undo:{event_id}"}]])
    elif action == "undo":
        if event_id in state["done"]:
            state["done"].remove(event_id)
            title = event["title"] if event else "המטלה"
            answer_button(cq, "בוטל")
            send(f"↩️ החזרתי לרשימה: <b>{html.escape(title)}</b>")
        else:
            answer_button(cq, "אין מה לבטל")


def reply_to_command(text: str, events: list[dict], state: dict, now: datetime):
    cmd = text.split()[0].lower() if text else ""
    if cmd in ("/help", "/start"):
        send(HELP)
    elif cmd == "/done":
        m = re.search(r"\d+", text)
        items = upcoming(events, state, now)
        if not m or not 1 <= int(m.group()) <= len(items):
            send("לא הבנתי איזו מטלה. שלח /tasks ולחץ על הכפתור של המטלה.")
            return
        e = items[int(m.group()) - 1]
        state["done"].append(e["id"])
        send(f"✅ סימנתי כמוגשת: <b>{html.escape(e['title'])}</b>",
             [[{"text": "↩️ ביטול", "callback_data": f"undo:{e['id']}"}]])
    elif cmd == "/undo":
        if not state["done"]:
            send("אין מה לבטל.")
            return
        undone = state["done"].pop()
        title = next((e["title"] for e in events if e["id"] == undone), "המטלה")
        send(f"↩️ החזרתי לרשימה: <b>{html.escape(title)}</b>")
    else:
        send(*task_list(events, state, now))


# ---------- state ----------

def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("done", [])
    state.setdefault("tg_offset", 0)
    state.setdefault("seen", {})
    for event_id, entry in state["seen"].items():
        if isinstance(entry, list):
            # Old format: just the reminder thresholds already sent.
            # The due date is filled in on the next calendar read.
            state["seen"][event_id] = {"due": None, "sent": sorted(entry), "announced": True}
    return state


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")


# ---------- main logic ----------

def process(events: list[dict], state: dict | None, now: datetime) -> dict:
    first_run = state is None
    if first_run:
        state = {"seen": {}, "done": [], "tg_offset": 0}
    seen = state["seen"]  # id -> {due, sent, announced, old_due?}

    for e in sorted(events, key=lambda e: e["due"]):
        due_ts = int(e["due"].timestamp())
        hours_left = (e["due"] - now).total_seconds() / 3600
        passed = [h for h in REMINDER_HOURS if hours_left <= h]
        entry = seen.get(e["id"])

        if entry is None:
            # Reminder thresholds already passed are covered by the "new" message
            entry = seen[e["id"]] = {"due": due_ts, "sent": passed,
                                     "announced": first_run or hours_left <= 0}
        elif entry["due"] is None:
            entry["due"] = due_ts  # migrated from the old state format
        elif abs(entry["due"] - due_ts) > DUE_TOLERANCE:
            entry.setdefault("old_due", entry["due"])
            entry["due"] = due_ts
            if abs(entry["old_due"] - due_ts) <= DUE_TOLERANCE:
                del entry["old_due"]  # changed back before you were told

        if e["id"] in state["done"] or hours_left <= 0 or not can_send_about(e, now):
            continue  # pending messages wait (quiet hours) or are no longer needed

        if not entry["announced"]:
            send(f"🆕 <b>מטלה חדשה במודל!</b>\n\n{fmt_event(e, now)}", done_button(e))
            entry["announced"] = True
            entry.pop("old_due", None)
            entry["sent"] = passed
        elif "old_due" in entry:
            send(f"📅 <b>מועד ההגשה השתנה!</b>\n\n{fmt_event(e, now)}\n\n"
                 f"<s>היה: {fmt_due(from_ts(entry['old_due']))}</s>", done_button(e))
            del entry["old_due"]
            entry["sent"] = passed  # reminders start over for the new date
        elif any(h not in entry["sent"] for h in passed):
            send(f"⏰ <b>תזכורת: מתקרבת הגשה!</b>\n\n{fmt_event(e, now)}", done_button(e))
            entry["sent"] = sorted(set(entry["sent"]) | set(passed))

    local = now.astimezone(LOCAL_TZ)
    if first_run:
        send("✅ הבוט מחובר למודל!\n\n" + HELP)
        send(*task_list(events, state, now))
        if local.weekday() == DIGEST_WEEKDAY:
            state["last_digest"] = local.date().isoformat()
    elif (local.weekday() == DIGEST_WEEKDAY and not is_quiet(now)
          and state.get("last_digest") != local.date().isoformat()):
        send(*weekly_digest(events, state, now))
        state["last_digest"] = local.date().isoformat()

    # Forget events that ended over two weeks ago so the state file stays small
    alive = {e["id"] for e in events if e["due"] > now - timedelta(days=14)}
    state["seen"] = {k: v for k, v in seen.items() if k in alive}
    state["done"] = [d for d in state["done"] if d in alive]
    return state


def keepalive(state: dict, now: datetime):
    """Change the state now and then, so the scheduled workflow commits something.

    GitHub turns off schedules in public repositories after 60 days without
    activity, which could happen in a semester break with no new assignments.
    """
    today = now.astimezone(LOCAL_TZ).date()
    last = state.get("keepalive")
    if not last or (today - date.fromisoformat(last)).days >= KEEPALIVE_DAYS:
        state["keepalive"] = today.isoformat()


def calendar_failed(state: dict | None, now: datetime, exc: Exception):
    print(f"Could not read Moodle calendar: {exc}", file=sys.stderr)
    if state is None:
        return
    if "fail_since" not in state:
        state["fail_since"] = int(now.timestamp())
    failing_for = now - from_ts(state["fail_since"])
    if failing_for >= FAIL_ALERT_AFTER and not state.get("fail_alerted") and not is_quiet(now):
        try:
            send("⚠️ <b>אני לא מצליח לקרוא את היומן מהמודל</b>\n\n"
                 "עד שזה יסתדר לא תקבל ממני התראות על מטלות. אם זה לא עובר תוך כמה שעות, "
                 "ייתכן שהקישור לייצוא היומן פג: ייצא קישור חדש במודל (לוח שנה ← ייצוא) "
                 "ועדכן את הסוד MOODLE_ICAL_URL בריפו.\n\n"
                 f"<i>שגיאה: {html.escape(str(exc)[:200])}</i>")
        except Exception as send_exc:
            print(f"Could not send the alert either: {send_exc}", file=sys.stderr)
            return
        state["fail_alerted"] = True


def calendar_ok(state: dict | None):
    if state is None:
        return
    state.pop("fail_since", None)
    if state.pop("fail_alerted", None):
        send("✅ <b>חזרתי לקרוא את היומן מהמודל.</b> ההתראות חוזרות לעבוד כרגיל.")


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    try:
        events = parse_events(fetch_calendar())
    except Exception as exc:
        calendar_failed(state, now, exc)
        if state is not None:
            save_state(state)
        sys.exit(1)
    try:
        calendar_ok(state)
        state = process(events, state, now)
        keepalive(state, now)
        handle_messages(events, state, now)
    finally:
        # Everything is recorded only after its message was sent, so saving
        # after an error never marks an unsent message as sent
        if state is not None:
            save_state(state)


if __name__ == "__main__":
    main()
