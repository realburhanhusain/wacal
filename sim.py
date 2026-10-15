"""Console simulator. Drives the real command layer with no Meta account.

    python sim.py                      # peer mode, scratch database
    python sim.py --db wacal.db        # keep state between runs
    python sim.py --fake-google        # pretend a Google Calendar is linked

Inside the prompt:
    /as <phone> [name]   switch to another WhatsApp number
    /who                 list known numbers
    /seed-clinic         create a demo business booking page
    /host <slug>         route messages to that business page
    /peer                back to peer mode
    /gcal <HH:MM-HH:MM>  set the fake Google busy block for today (host owner)
    /quit
A reply with rows is numbered: type the number to pick it, or "yes" to
confirm. English, Hindi and Arabic aliases work -- try "नमस्ते".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from wacal import db, gcal, numbering
from wacal.business import handle_business
from wacal.commands import handle
from wacal.slots import tz_of
from wacal.whatsapp import Inbound

SEED = [("+966500000001", "Burhan"), ("+966500000002", "Ahmed")]
CLINIC_SERVICES = [
    {"name": "Consultation", "duration_mins": 30, "price": 150},
    {"name": "Follow-up", "duration_mins": 15, "price": 50},
]

# A Windows console defaults to cp1252. Output needs UTF-8 for '·' and '—';
# input needs it too, or a typed Hindi alias arrives mangled and falls through
# to the help text instead of matching wacal.keywords.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


class FakeGoogle:
    """A stand-in for Google Calendar so the mirror path can be demonstrated."""

    def __init__(self) -> None:
        # Named with an underscore because `busy` is the method below.
        self._busy: list[tuple[str, str]] = []
        self.events: dict[str, dict] = {}
        self.counter = 0

    def busy(self, token_json, calendar_id, start, end, *, on_refresh=None):
        return list(self._busy)

    def list_events(self, token_json, calendar_id, start, end, *, limit=50,
                    on_refresh=None):
        return [{"id": event_id, "summary": event["title"],
                 "start": event["start"].isoformat(),
                 "end": event["end"].isoformat()}
                for event_id, event in self.events.items()]

    def create_event(self, token_json, calendar_id, *, start, end, title,
                     description="", location="", on_refresh=None):
        self.counter += 1
        event_id = f"fake-google-{self.counter}"
        self.events[event_id] = {"start": start, "end": end, "title": title}
        print(f"    [google] created {event_id}: {title}")
        return event_id

    def delete_event(self, token_json, calendar_id, external_id, *,
                     on_refresh=None):
        self.events.pop(external_id, None)
        print(f"    [google] deleted {external_id}")


def show(label: str, reply) -> None:
    print(f"\n--- to {label} ---")
    print(reply.text)
    for index, row in enumerate(reply.rows, start=1):
        print(f"  [{index}] {row.title}  {row.description}")


def seed_clinic(conn):
    owner = db.get_user_by_phone(conn, SEED[0][0])
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    if db.get_host(conn, "demo-clinic") is None:
        db.create_host(conn, "demo-clinic", owner["id"], "Demo Clinic",
                       location="123 King Fahd Road", currency="SAR",
                       services=json.dumps(CLINIC_SERVICES))
    return db.get_host(conn, "demo-clinic")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--fake-google", action="store_true")
    args = parser.parse_args()

    conn = db.connect(args.db)
    for phone, name in SEED:
        if db.get_user_by_phone(conn, phone) is None:
            db.create_user(conn, phone, name, "Asia/Riyadh")

    fake = FakeGoogle()
    if args.fake_google:
        gcal.PROVIDER = fake
        owner = db.get_user_by_phone(conn, SEED[0][0])
        db.save_calendar_link(conn, owner["id"], '{"token": "fake"}',
                              account="demo@example.com")
        print(f"Fake Google Calendar linked to {owner['display_name']}.")

    me = SEED[0][0]
    profile = dict(SEED)
    host = None
    counter = 0

    print(__doc__)
    print(f"You are {profile[me]} {me}. Try: share {SEED[1][0]}")

    while True:
        where = f" @{host['slug']}" if host else ""
        try:
            line = input(f"\n[{profile.get(me, me)}{where}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line == "/who":
            for row in conn.execute("SELECT phone, display_name FROM users"):
                print(f"  {row['phone']}  {row['display_name']}")
            continue
        if line == "/seed-clinic":
            created = seed_clinic(conn)
            print(f"Created {created['business_name']} at /webhook/{created['slug']}")
            continue
        if line == "/peer":
            host = None
            print("Peer mode.")
            continue
        if line.startswith("/host"):
            slug = line.partition(" ")[2].strip() or "demo-clinic"
            host = db.get_host(conn, slug)
            print(f"Business mode: {host['business_name']}" if host
                  else f"No host called {slug}. Try /seed-clinic")
            continue
        if line.startswith("/gcal"):
            window = line.partition(" ")[2].strip()
            start_text, _, end_text = window.partition("-")
            owner = db.get_user_by_phone(conn, SEED[0][0])
            zone = tz_of(owner["timezone"])
            today = datetime.now(zone).date()
            try:
                start = datetime.combine(
                    today, datetime.strptime(start_text, "%H:%M").time(),
                    tzinfo=zone)
                end = datetime.combine(
                    today, datetime.strptime(end_text, "%H:%M").time(),
                    tzinfo=zone)
            except ValueError:
                print("Usage: /gcal 10:00-11:00")
                continue
            fake._busy = [(start.astimezone(timezone.utc).isoformat(),
                           end.astimezone(timezone.utc).isoformat())]
            print(f"Fake Google busy {start:%H:%M}-{end:%H:%M} {owner['timezone']}")
            continue
        if line.startswith("/as "):
            parts = line.split()
            me = parts[1]
            if len(parts) > 2:
                profile[me] = " ".join(parts[2:])
            existing = db.get_user_by_phone(conn, me)
            if existing:
                profile[me] = existing["display_name"]
            print(f"Now sending as {profile.get(me, me)} {me}")
            continue

        counter += 1
        message = Inbound(from_phone=me, text=line, message_id=f"sim.{counter}",
                          profile_name=profile.get(me, ""), is_tap=False)
        # Same path the webhook uses: a typed number stands in for a tap.
        # Same scoping the webhook uses: one list per conversation, so two
        # hosts cannot share a numbered menu.
        scope = host["slug"] if host else numbering.PEER_SCOPE
        message = numbering.resolve(conn, message, scope)
        if host is not None:
            outcome = handle_business(conn, db.get_host(conn, host["slug"]), message)
        else:
            outcome = handle(conn, message)
        numbering.remember(conn, message.from_phone, scope, outcome.reply)

        show(profile.get(me, me), outcome.reply)
        for phone, note in outcome.notify:
            target = db.get_user_by_phone(conn, phone)
            show(target["display_name"] if target else phone, note)


if __name__ == "__main__":
    sys.exit(main())
