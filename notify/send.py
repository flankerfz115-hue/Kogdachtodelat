"""Запускается GitHub Actions каждые 5 минут: решает, какие уведомления пора отправить, и шлёт пуши.

Использование: python notify/send.py <папка с веткой data>
Переменные окружения: DATA_KEY (секрет репозитория), TEST=true — отправить тестовое уведомление.
"""
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

LOOKBACK = timedelta(minutes=60)   # запуски GitHub Actions иногда опаздывают — не теряем события за последний час
STEP = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


def decrypt_state(path, key):
    blob = json.loads(path.read_text())
    plain = AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None)
    return json.loads(plain)


def lessons_for(state, day):
    subjects = state.get("timetable", {}).get(str(day.weekday()), [])
    out = []
    for i, (start, end) in enumerate(state.get("bells", [])):
        item = subjects[i] if i < len(subjects) else None
        if item and (item.get("subject") or "").strip():
            out.append({"n": i + 1, "start": start, "end": end,
                        "subject": item["subject"].strip(), "room": (item.get("room") or "").strip()})
    return out


def due_events(state, now):
    """Список (ключ, дата ключа, заголовок, текст) для всего, что пора отправить."""
    events = []

    for r in state.get("reminders", []):
        due = datetime.fromisoformat(r["due_at"][:16])
        step = STEP.get(r.get("repeat"))
        if step and due <= now:
            due += step * ((now - due) // step)
        if now - LOOKBACK < due <= now:
            events.append((f"rem:{r['id']}:{due.isoformat()}", due.date(), "⏰ " + r["text"], f"Напоминание на {due:%H:%M}"))

    if not state.get("school_on", True):
        return events
    today = now.date()
    lessons = lessons_for(state, today)
    if not lessons:
        return events

    morning = state.get("morning_at")
    if morning:
        m = datetime.combine(today, time.fromisoformat(morning))
        if m <= now < m + LOOKBACK:
            first = lessons[0]
            body = "\n".join(f"{l['start']} {l['subject']}" for l in lessons)
            events.append(("morning", today, f"🗓 Сегодня {len(lessons)} ур., первый в {first['start']}", body))

    before = timedelta(minutes=int(state.get("notify_before", 5)))
    for i, l in enumerate(lessons):
        start = datetime.combine(today, time.fromisoformat(l["start"]))
        end = datetime.combine(today, time.fromisoformat(l["end"]))
        if not (start - before <= now < end):
            continue
        parts = [f"{l['n']}-й урок"]
        if l["room"]:
            parts.append(f"каб. {l['room']}")
        nxt = lessons[i + 1] if i + 1 < len(lessons) else None
        parts.append(f"дальше {nxt['subject']}" if nxt else "последний урок")
        events.append((f"lesson:{l['n']}", today, f"{l['start']} — {l['subject']}", " · ".join(parts)))

    return events


def main():
    data_dir = Path(sys.argv[1])
    state_file = data_dir / "state.enc.json"
    if not state_file.exists():
        print("Данных ещё нет — подключи GitHub в приложении")
        return
    key_b64 = os.environ.get("DATA_KEY", "")
    if not key_b64:
        sys.exit("Нет секрета DATA_KEY: добавь его в Settings → Secrets and variables → Actions")
    key = base64.b64decode(key_b64)
    state = decrypt_state(state_file, key)

    now = datetime.now(ZoneInfo(state.get("tz") or "Europe/Moscow")).replace(tzinfo=None)
    sent_file = data_dir / "sent.json"
    sent = set(json.loads(sent_file.read_text())) if sent_file.exists() else set()

    to_send = []
    for event_key, day, title, body in due_events(state, now):
        # в публичном репозитории храним только хэши, чтобы по ним нельзя было понять расписание
        h = f"{day.isoformat()}:{hashlib.sha256(key + event_key.encode()).hexdigest()[:16]}"
        if h not in sent:
            sent.add(h)
            to_send.append((title, body, event_key))
    if os.environ.get("TEST") == "true":
        to_send.append(("✅ Уведомления работают", "Так будут приходить напоминания и расписание", "test"))

    if to_send:
        send_push(state, to_send)

    keep_from = (now.date() - timedelta(days=2)).isoformat()
    sent = sorted(k for k in sent if k[:10] >= keep_from)
    old = json.loads(sent_file.read_text()) if sent_file.exists() else []
    if sent != old:
        sent_file.write_text(json.dumps(sent, indent=0))
    print(f"{now:%Y-%m-%d %H:%M}: отправлено {len(to_send)}")


def send_push(state, messages):
    from pywebpush import WebPushException, webpush

    subs = state.get("subscriptions", [])
    vapid = state.get("vapid") or {}
    if not subs or not vapid.get("privateKey"):
        print("Нет подписок на уведомления — включи их в приложении")
        return
    # Apple требует в sub адрес сайта (без пути) или mailto:
    host = urlparse(state.get("site") or "").netloc
    claims = {"sub": f"https://{host}" if host else "mailto:noreply@example.com"}
    for title, body, tag in messages:
        payload = json.dumps({"title": title, "body": body, "tag": tag}, ensure_ascii=False)
        for sub in subs:
            try:
                webpush(sub, payload, vapid_private_key=vapid["privateKey"], vapid_claims=dict(claims), ttl=3600)
            except WebPushException as e:
                status = e.response.status_code if e.response is not None else "?"
                print(f"Пуш не доставлен ({status}) на {sub['endpoint'][:40]}…")


if __name__ == "__main__":
    main()
