#!/usr/bin/env python3
"""Initialise the database, seed a demo booking page, then serve.

    python start.py              # http://localhost:8000
    python start.py --port 9000 --no-demo

Seeding is idempotent: an existing demo host is left untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from wacal import db

DEMO_SLUG = "demo-clinic"
DEMO_OWNER = "+966500000001"
DEMO_SERVICES = [
    {"name": "Consultation", "duration_mins": 30, "price": 150},
    {"name": "Follow-up", "duration_mins": 15, "price": 50},
    {"name": "Full checkup", "duration_mins": 60, "price": 400},
]


def seed(conn) -> None:
    owner = db.get_user_by_phone(conn, DEMO_OWNER)
    if owner is None:
        owner = db.create_user(conn, DEMO_OWNER, "Demo Clinic", "Asia/Riyadh")
    if db.get_host(conn, DEMO_SLUG) is None:
        db.create_host(
            conn, DEMO_SLUG, owner["id"], "Demo Clinic",
            location="123 King Fahd Road", currency="SAR",
            services=json.dumps(DEMO_SERVICES),
            work_start="09:00", work_end="19:00",
            work_days="0,1,2,3,4,5", slot_minutes=30,
        )
        print(f"Seeded demo host '{DEMO_SLUG}' owned by {DEMO_OWNER}")
    else:
        print(f"Demo host '{DEMO_SLUG}' already present")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--no-demo", action="store_true")
    args = parser.parse_args()

    # db.connect() already migrates, so report what it did rather than calling
    # migrate() again and always printing an empty list.
    applied: list[str] = []
    conn = db.connect(on_migrate=applied.extend)
    print(f"Database ready at {db.DEFAULT_DB}")
    if applied:
        print(f"Applied migrations: {', '.join(applied)}")
    pruned = db.prune(conn)
    if any(pruned.values()):
        print(f"Pruned: {pruned}")
    if not args.no_demo:
        seed(conn)
    conn.close()

    base = os.environ.get("BASE_URL", f"http://{args.host}:{args.port}")
    transport = os.environ.get("WA_TRANSPORT") or "auto"
    print()
    print(f"Cloud API webhook   {base}/webhook/{DEMO_SLUG}")
    print(f"Twilio webhook      {base}/twilio/webhook/{DEMO_SLUG}")
    print(f"Peer-mode webhook   {base}/webhook")
    print(f"Dashboard API       {base}/dashboard/{DEMO_SLUG}  (x-api-key)")
    print(f"API docs            {base}/docs")
    print(f"Transport           {transport}")
    if not os.environ.get("ADMIN_KEY"):
        print("ADMIN_KEY is unset, so every /dashboard route will answer 503.")
    print()

    # Without this the banner is still buffered when uvicorn takes the
    # terminal, so it appears after the server's own startup lines.
    sys.stdout.flush()
    command = [sys.executable, "-m", "uvicorn", "wacal.main:app",
               "--host", args.host, "--port", str(args.port)]
    if args.reload:
        command.append("--reload")
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
