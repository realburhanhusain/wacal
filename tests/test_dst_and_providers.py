"""DST edges in `block`, the provider registry, and per-host timezones."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone

import pytest
from fastapi.testclient import TestClient

from wacal import db, gcal, main, providers
from wacal.business import handle_business
from wacal.commands import handle
from wacal.slots import is_ambiguous, is_skipped, offset_label, schedule_for, to_utc
from wacal.whatsapp import Inbound, MemoryTransport

LONDON = "Europe/London"
RIYADH = "Asia/Riyadh"
# Autumn 2026: clocks go back in London on 25 October, forward on 29 March.
FALL_BACK = date(2026, 10, 25)
SPRING_FORWARD = date(2026, 3, 29)
EARLY = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
SERVICES = [{"name": "Consultation", "duration_mins": 30, "price": 150}]


@pytest.fixture()
def londoner(conn):
    user = db.create_user(conn, "+447700900001", "Burhan", LONDON)
    db.update_user(conn, user["id"], work_days="0,1,2,3,4,5,6",
                   work_start="00:00", work_end="23:30")
    return db.get_user(conn, user["id"])


def say(conn, text, *, now=EARLY, phone="+447700900001", counter=[0]):
    counter[0] += 1
    return handle(conn, Inbound(from_phone=phone, text=text,
                                message_id=f"d{counter[0]}"), now=now).reply.text


# --- the helpers ---------------------------------------------------------


def test_ambiguity_and_skips_are_detected():
    assert is_ambiguous(FALL_BACK, time(1, 30), LONDON) is True
    assert is_ambiguous(FALL_BACK, time(4, 0), LONDON) is False
    assert is_skipped(SPRING_FORWARD, time(1, 30), LONDON) is True
    assert is_skipped(SPRING_FORWARD, time(4, 0), LONDON) is False
    # A zone without DST has neither.
    assert is_ambiguous(FALL_BACK, time(1, 30), RIYADH) is False
    assert is_skipped(SPRING_FORWARD, time(1, 30), RIYADH) is False


def test_the_two_folds_are_different_instants():
    first = to_utc(FALL_BACK, time(1, 30), LONDON, fold=0)
    second = to_utc(FALL_BACK, time(1, 30), LONDON, fold=1)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert second == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert offset_label(first, LONDON) == "+0100"
    assert offset_label(second, LONDON) == "+0000"


# --- block across a transition ------------------------------------------


def test_a_time_that_does_not_exist_is_refused(conn, londoner):
    reply = say(conn, f"block {SPRING_FORWARD.isoformat()} 01:30-02:00 Gone")
    assert "does not exist" in reply
    assert "clocks go forward" in reply
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0


def test_both_halves_of_a_repeated_hour_can_be_blocked(conn, londoner):
    """Defaulting to fold 0 silently made the second hour unbookable."""
    first = say(conn, f"block {FALL_BACK.isoformat()} 01:30-02:00 Focus")
    assert "Blocked" in first
    assert "happens twice" in first
    assert "+0000 on the end" in first

    second = say(conn, f"block {FALL_BACK.isoformat()} 01:30-02:00 Focus +0000")
    assert "Blocked" in second

    rows = conn.execute(
        "SELECT start_utc, end_utc FROM events ORDER BY start_utc").fetchall()
    assert [(r["start_utc"], r["end_utc"]) for r in rows] == [
        ("2026-10-25T00:30:00+00:00", "2026-10-25T01:00:00+00:00"),
        ("2026-10-25T01:30:00+00:00", "2026-10-25T02:00:00+00:00"),
    ]


def test_the_duration_is_the_wall_clock_difference(conn, londoner):
    """Converting the end time independently gave 90 minutes, not 30.

    On a fall-back night 02:00 is unambiguous while 01:30 is not, so resolving
    each end separately stretched a half-hour block across the repeated hour.
    """
    say(conn, f"block {FALL_BACK.isoformat()} 01:30-02:00 Focus")
    row = conn.execute("SELECT start_utc, end_utc FROM events").fetchone()
    start = datetime.fromisoformat(row["start_utc"])
    end = datetime.fromisoformat(row["end_utc"])
    assert (end - start).total_seconds() == 30 * 60


def test_an_offset_that_is_not_in_effect_is_refused(conn, londoner):
    reply = say(conn, f"block {FALL_BACK.isoformat()} 01:30-02:00 X +0500")
    assert "not one of the two offsets" in reply
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0


def test_the_offset_is_not_taken_as_part_of_the_title(conn, londoner):
    say(conn, f"block {FALL_BACK.isoformat()} 01:30-02:00 Deep work +0000")
    assert conn.execute("SELECT title FROM events").fetchone()["title"] \
        == "Deep work"


def test_an_ordinary_day_says_nothing_about_dst(conn, londoner):
    reply = say(conn, "block 2026-06-10 10:00-11:00 Normal")
    assert "Blocked" in reply
    assert "happens twice" not in reply
    assert "+01" not in reply


# --- the provider registry ----------------------------------------------


def test_google_is_registered():
    assert providers.available() == ["google"]
    assert providers.for_name("google") is gcal.PROVIDER
    assert providers.for_name(None) is gcal.PROVIDER
    assert providers.for_name("GOOGLE") is gcal.PROVIDER


def test_the_registry_follows_the_test_seam(fake_google):
    """for_name resolves per call, so monkeypatching gcal.PROVIDER works."""
    assert providers.for_name("google") is fake_google


def test_an_unknown_provider_is_an_error_not_a_wrong_answer():
    with pytest.raises(providers.UnknownProvider, match="not supported"):
        providers.for_name("outlook")


def test_a_stored_row_decides_which_provider_is_used(conn, people,
                                                     fake_google):
    """The column was written and never read, so any row got Google."""
    owner, _ = people
    db.save_calendar_link(conn, owner["id"], '{"token": "x"}',
                          provider="outlook")
    link = db.get_calendar_link(conn, owner["id"])
    with pytest.raises(providers.UnknownProvider):
        providers.for_link(link)

    db.save_calendar_link(conn, owner["id"], '{"token": "x"}',
                          provider="google")
    assert providers.for_link(
        db.get_calendar_link(conn, owner["id"])) is fake_google


def test_an_unsupported_provider_surfaces_when_reading_availability(
        conn, people, fake_google):
    from wacal.busy import busy_for

    owner, _ = people
    db.save_calendar_link(conn, owner["id"], '{"token": "x"}',
                          provider="outlook")
    with pytest.raises(providers.UnknownProvider):
        busy_for(conn, owner["id"], EARLY, EARLY)


# --- per-host timezone ---------------------------------------------------


def test_a_host_timezone_overrides_the_owners(conn):
    owner = db.create_user(conn, "+966500000001", "Chain", RIYADH)
    host = db.create_host(conn, "london-branch", owner["id"], "London Branch",
                          timezone=LONDON)
    schedule = schedule_for(db.get_user(conn, owner["id"]), host)
    assert schedule["timezone"] == LONDON
    # Hours still inherit, since the host set none.
    assert schedule["work_start"] == owner["work_start"]


def test_an_unset_host_timezone_inherits(conn):
    owner = db.create_user(conn, "+966500000001", "Chain", RIYADH)
    host = db.create_host(conn, "home", owner["id"], "Home")
    assert schedule_for(db.get_user(conn, owner["id"]), host)["timezone"] \
        == RIYADH


def test_two_branches_in_two_cities_offer_different_times(conn):
    # Midnight UTC, so neither branch's working day has started yet. At 09:00
    # UTC the Riyadh morning is already past and both would answer with the
    # same later slot, which says nothing about the timezone.
    midnight = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    owner = db.create_user(conn, "+966500000001", "Chain", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    db.create_user(conn, "+966500000055", "Ravi", RIYADH)

    riyadh = db.create_host(conn, "riyadh", owner["id"], "Riyadh Branch",
                            services=json.dumps(SERVICES),
                            work_start="09:00", work_end="17:00",
                            work_days="0,1,2,3,4,5,6")
    london = db.create_host(conn, "london", owner["id"], "London Branch",
                            services=json.dumps(SERVICES), timezone=LONDON,
                            work_start="09:00", work_end="17:00",
                            work_days="0,1,2,3,4,5,6")

    def first_slot_utc(host):
        counter = [0]

        def send(text, tap=False):
            counter[0] += 1
            return handle_business(
                conn, db.get_host(conn, host["slug"]),
                Inbound(from_phone="+966500000055", text=text,
                        message_id=f"{host['slug']}{counter[0]}",
                        profile_name="Ravi", is_tap=tap),
                now=midnight,
            )

        service = send("hi").reply.rows[0]
        dates = send(service.id, tap=True).reply.rows[0]
        slots = send(dates.id, tap=True).reply.rows
        return int(slots[0].id.rsplit(":", 1)[-1])

    # 09:00 Riyadh is 06:00 UTC; 09:00 London on 1 March is 09:00 UTC.
    riyadh_first = first_slot_utc(riyadh)
    london_first = first_slot_utc(london)
    assert riyadh_first == int(
        datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc).timestamp())
    assert london_first - riyadh_first == 3 * 3600


def test_the_admin_api_reports_and_edits_the_host_timezone(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "k")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    try:
        with TestClient(main.app) as client:
            headers = {"x-api-key": "k"}
            created = client.post("/dashboard/register", headers=headers, json={
                "slug": "branch",
                "business_name": "Branch",
                "owner_phone": "+966500000001",
                "timezone": RIYADH,
                "services": SERVICES,
            })
            assert created.status_code == 201

            view = client.get("/dashboard/branch", headers=headers).json()
            assert view["timezone"] == RIYADH
            assert view["timezone_source"] == "host"

            patched = client.patch("/dashboard/branch", headers=headers,
                                   json={"timezone": LONDON})
            assert patched.status_code == 200
            assert client.get("/dashboard/branch", headers=headers
                              ).json()["timezone"] == LONDON

            bad = client.patch("/dashboard/branch", headers=headers,
                               json={"timezone": "Mars/Olympus"})
            assert bad.status_code == 422
    finally:
        main.app.state.conn.close()
        main.app.state.conn = None
        main.app.state.transport = None
