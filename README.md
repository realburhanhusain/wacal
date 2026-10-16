# wacal

Availability and booking over WhatsApp, in two modes.

**Peer mode** — colleagues see each other's free time and block or unblock it.
Default deny: an owner must share before anyone can look.

**Business mode** — a public booking page per host, reachable at
`/webhook/{slug}`. Customers pick a service, a date and a time. The link is the
authorisation, so no sharing step applies.

Both modes run on one calendar engine and one database. Google Calendar is an
optional second busy source, per user, in either mode. Either transport works:
Meta Cloud API with tappable rows, or Twilio with numbered replies.

## How it works

```
WhatsApp user
   |  POST /webhook                 peer mode, Meta Cloud API
   |  POST /webhook/{slug}          business mode, Meta Cloud API
   |  POST /twilio/webhook          peer mode, Twilio
   |  POST /twilio/webhook/{slug}   business mode, Twilio
   v
wacal.main          signature check, message-id dedupe, route by slug
   |
   +-> wacal.numbering   a typed "3" stands in for tapping row 3
   +-> wacal.keywords    phrase aliases, English / Hindi / Arabic
   |
   +-> wacal.commands    peer: share / free / book / block / cancel
   +-> wacal.business    customer: service -> date -> time -> confirm
         |
         v
wacal.busy          local busy intervals + linked Google Calendar
wacal.slots         working hours -> slot grid, minus busy
wacal.db            SQLite: users, grants, events, hosts, calendar_links
wacal.gcal          OAuth, freebusy, mirror create/delete
   v
wacal.whatsapp      reply as text, or as an interactive list
```

## Peer mode

```
share +966500000002      # grant free/busy visibility and the right to book
unshare +966500000002
shared                   # who can book you, and who you can book
```

| Command | Effect |
| --- | --- |
| `free ahmed tomorrow` | Slots free for both of you, as a tappable list |
| `free ahmed` | Rolls forward to the next day with an opening (14 days) |
| tap a slot | Books it, notifies the owner, returns a ref |
| `block tomorrow 14:00-15:00 Focus` | Blocks your own time |
| `block 2026-10-25 01:30-02:00 X +0000` | Picks a side of a repeated hour |
| `closed 2026-12-25` | Blocks a whole working day, holidays included |
| `my` | Upcoming bookings with their refs |
| `cancel ABC123` | Unblocks the slot and tells the other party |
| `connect google` / `disconnect google` | Link an external calendar |
| `mylink` | Your business booking page, if you have one |
| `name` / `tz` / `hours` / `days` / `me` | Your settings |

A host owner reaches all of these on their business number too: one WhatsApp
number maps to one webhook, so `/webhook/{slug}` delegates any non-booking
command from the owner to the peer handler.

Aliases are accepted for the common verbs, in English, Hindi and Arabic:
`hi` / `नमस्ते` / `مرحبا`, `my bookings` / `मेरी बुकिंग`, `cancel` / `रद्द करें`,
`yes` / `हाँ`, `free` / `खाली`. See `wacal/keywords.py`.

Slots are offered on the owner's grid and inside the owner's working hours,
filtered against the requester's bookings and working window, then rendered in
the requester's timezone. Everything is stored in UTC. Free/busy exposes
intervals only — event titles are never shown to a viewer.

## Business mode

Register a host, then point a WhatsApp number's webhook at its slug:

```bash
curl -X POST $BASE_URL/dashboard/register \
  -H "x-api-key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{
    "slug": "dr-sharma",
    "business_name": "Dr Sharma Clinic",
    "owner_phone": "+966500000001",
    "location": "123 King Fahd Road",
    "timezone": "Asia/Riyadh",
    "currency": "SAR",
    "services": [
      {"name": "Consultation", "duration_mins": 30, "price": 150},
      {"name": "Follow-up",    "duration_mins": 15, "price": 50}
    ],
    "working_days": [0,1,2,3,4,5,6],
    "working_hours": {"start": "09:00", "end": "17:00"},
    "slot_minutes": 30,
    "owner_email": "clinic@example.com"
  }'
```

Opening hours **and the timezone** live on the host, not on the owner, so one
person can run two businesses in different cities. Any host field left null
inherits the owner's; `GET /dashboard/{slug}` reports which applies as
`schedule_source` and `timezone_source`.

The customer conversation. On Cloud API the rows are tappable; on Twilio the
same rows arrive numbered and the customer replies `1`, or `yes` to confirm:

```
Customer: hi
Bot:      Welcome to Dr Sharma Clinic, Ravi. Choose a service.
          [Consultation  30 min · 150 SAR] [Follow-up  15 min · 50 SAR]
Customer: (taps Consultation)
Bot:      Choose a date.  [Thu 15 Oct (today) 14 slot(s) open] ...
Customer: (taps a date)
Bot:      Open times ... [09:30-10:00] [10:00-10:30] ...
Customer: (taps a time)
Bot:      Please confirm. Name / Phone / Service / Price / When / Where
          [Confirm] [Start over]
Customer: (taps Confirm)
Bot:      Confirmed. Ref 7KQ2MD · to cancel send: cancel 7KQ2MD
Owner:    New booking at Dr Sharma Clinic. Ravi (+966...) Consultation · 150 SAR
```

Slot length follows the chosen service. The customer's own working hours are
ignored — a patient is not at work — but their existing bookings still are. A
new customer inherits the host's timezone, since nobody arrives having set one.
The owner texting their own page gets the next 7 days of appointments.

`reschedule` moves a booking: the old ref travels in the button ids and is
cancelled only once the replacement exists. Each id also carries a fingerprint
of the service it was built for, so editing the menu between two taps
invalidates the old button instead of silently booking something else.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/` | GET | Service banner and every webhook path |
| `/health` | GET | Liveness |
| `/webhook` | POST | Peer mode, Meta Cloud API |
| `/webhook/{slug}` | POST | Business mode, Meta Cloud API |
| `/twilio/webhook` | POST | Peer mode, Twilio |
| `/twilio/webhook/{slug}` | POST | Business mode, Twilio |
| `/webhook/{slug}/status` | GET | Public health check for that page |
| `/dashboard/register` | POST | Create a host (x-api-key) |
| `/dashboard/{slug}` | GET | Full host configuration |
| `/dashboard/{slug}` | PUT | Replace the configuration, hours included |
| `/dashboard/{slug}` | PATCH | Change only the fields sent |
| `/dashboard/{slug}/bookings` | GET | Upcoming bookings |
| `/dashboard/{slug}/stats` | GET | Upcoming 7/30 day counts, plus confirmed, cancelled, distinct customers, cancellation rate and a per-service breakdown |
| `/dashboard/{slug}/calendar` | GET | The owner's own Google events, titles included |
| `/auth/google/callback` | GET | OAuth redirect target |
| `/docs` | GET | Swagger UI |

`ADMIN_KEY` unset makes every `/dashboard/*` route answer 503. An admin API
that opens itself when a variable is missing is worse than no admin API. The
same rule applies to the webhooks: see [SECURITY.md](SECURITY.md).

## Google Calendar

Opt-in, per user, in either mode. The user sends `connect google` and gets a
link carrying a signed 15-minute state token bound to their user id. There is
no password login anywhere: Meta has already authenticated the phone number.

Once linked:

- Google events block slots here.
- Bookings made here are written to Google, and the returned event id is
  stored, so cancelling here deletes the event there.
- A refreshed access token is written back to the database.

Two failure behaviours are deliberate:

- **An unreachable Google means no slots are offered**, and the user is told
  why. Falling back to "assume free" is how a booking lands on top of a real
  meeting.
- **A booking whose mirror write failed still stands here**, and the owner's
  message says the copy did not reach Google. Likewise a failed delete says so.
  Silent divergence between two calendars is the worst of the three outcomes.

Setup: create an OAuth 2.0 **Web application** client, enable the Google
Calendar API, add `${BASE_URL}/auth/google/callback` as an authorised redirect
URI, and save the JSON as `client_secrets.json`.

## Run it without any accounts

```
python sim.py                   # peer mode, throwaway database
python sim.py --db wacal.db     # persist between runs
python sim.py --fake-google     # a stubbed Google Calendar, prints its writes
```

```
[Burhan] > /seed-clinic
[Burhan] > /host demo-clinic
[Burhan] > /as +966500000002 Ahmed
[Ahmed @demo-clinic] > hi
[Ahmed @demo-clinic] > 1        # tap the first row
```

`/peer` returns to peer mode, `/gcal 10:00-11:30` sets the stubbed busy block,
`/who` lists numbers.

## Transports

`WA_TRANSPORT` is `cloud`, `twilio` or `memory`. Unset, whichever provider has
complete credentials wins, preferring Cloud API, and with neither the in-memory
transport keeps the app runnable. A forced choice with missing credentials
raises at startup rather than falling back silently.

| | Meta Cloud API | Twilio |
| --- | --- | --- |
| Webhook | `/webhook`, `/webhook/{slug}` | `/twilio/webhook`, `/twilio/webhook/{slug}` |
| Body | JSON, `X-Hub-Signature-256` | form post, `X-Twilio-Signature` |
| Menus | interactive list, real taps | numbered text, user replies `1` |
| Setup | Business verification needed | sandbox number works today |

Twilio has no interactive list message, so `wacal.whatsapp.render_numbered`
flattens the rows and `wacal.numbering` maps the typed number back to the row
id. Numbers work on Cloud API too, alongside the buttons.

Twilio signs the exact URL configured in its console. Behind a proxy that
terminates TLS, `str(request.url)` is `http://...` internally and every request
fails the check -- set `TWILIO_PUBLIC_URL` to the public value.

## The 24-hour window, and template messages

WhatsApp opens a 24-hour window when someone messages you and closes it 24
hours after their last message. Inside it, anything can be sent. Outside it
only a **template Meta approved in advance** is delivered; free-form text is
rejected with error 131047 and the message is gone.

This matters because the messages wacal sends outside a conversation are
exactly the important ones: "someone just booked you", "your booking was
cancelled", a verification code. The owner has usually not texted the app that
day, so their window is shut.

Each notification is a *kind*, and each kind reads its approved name from the
environment:

```
WA_TEMPLATE_BOOKING_CONFIRMED=wacal_booking_confirmed
WA_TEMPLATE_BOOKING_CANCELLED=wacal_booking_cancelled
WA_TEMPLATE_OTP=wacal_otp
WA_TEMPLATE_LANGUAGE=en                 # must match the approved language
```

Create these in WhatsApp Manager > Message templates with three body
variables, in this order:

| Kind | Category | `{{1}}` | `{{2}}` | `{{3}}` |
| --- | --- | --- | --- | --- |
| `booking_confirmed` | Utility | who booked what | when, with timezone | ref |
| `booking_cancelled` | Utility | who cancelled | when, with timezone | ref |
| `otp` | Authentication | the code | — | — |

A body of, for example, `New booking: {{1}}` / `When: {{2}}` / `Ref: {{3}}`.
The authentication template is built from Meta's own fixed copy; tick the
copy-code button and wacal passes the code to it as well as to the body.

Behaviour with nothing configured: a message to someone **inside** their
window still goes as normal text, and a message to someone **outside** it is
refused -- the outbox row stays pending with the reason in `last_error`, and
goes out by itself the moment that person next messages the number. Nothing is
lost, and nothing is reported as sent that was not. `python start.py` prints a
warning when no template is configured.

For local work, `WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW=1` sends anyway. Setting
it in production converts a visible refusal into an invisible drop.

On Twilio the same kinds are addressed by **Content template SID** (`HX...`),
not by name, so `WA_TEMPLATE_*` holds the SID there and variables are numbered
from 1.

## Verifying a phone number nobody has proved

An inbound message already proves its sender -- Meta signs the webhook. What
is unproved is a number somebody *types*: `POST /dashboard/register` names an
`owner_phone` which then receives other people's booking details, so a typo
leaks a stranger's calendar to a stranger.

With `WACAL_REQUIRE_OTP=1`, registration refuses an unverified number:

```
curl -XPOST $BASE/dashboard/verify/start   -H "x-api-key: $ADMIN_KEY" \
     -H 'content-type: application/json' -d '{"phone":"+966500000001"}'
curl -XPOST $BASE/dashboard/verify/confirm -H "x-api-key: $ADMIN_KEY" \
     -H 'content-type: application/json' -d '{"phone":"+9665...","code":"123456"}'
```

Six digits, ten minutes, five attempts, one resend a minute. The code is never
stored -- only an HMAC of it, keyed with `WACAL_OTP_PEPPER` or `WACAL_TOKEN_KEY`
and bound to the phone number, so a row copied onto another number verifies
nothing. The code is sent through the normal outbox, which means a number that
has never messaged the app needs `WA_TEMPLATE_OTP` to receive it at all.

## Finding your Meta ids

Four values live in four places in the dashboard, and all of them are
derivable from the access token:

```
python meta_ids.py <token>        # or set WA_TOKEN
python meta_ids.py --env          # as a .env block
```

It prints the app id, the token type and expiry, the scopes (and which
required ones are missing), every WABA id the token can act on, the phone
number id for each -- that is the id in the send URL, not the phone number --
and the templates already approved, with their status. It never prints the
token back.

## Run the service

```
pip install -r requirements.txt
cp .env.example .env            # then fill it in
python start.py                 # migrates, seeds a demo host, then serves
python start.py --no-demo --reload --port 9000
uvicorn wacal.main:app --port 8000     # or plain, no seeding
pytest -q                       # 262 tests
python tests/mutation_check.py  # proves the guards are actually tested
```

Expose port 8000 with `ngrok http 8000` or `cloudflared tunnel`. In the Meta
app dashboard set the callback URL, the verify token to `WA_VERIFY_TOKEN`, and
subscribe to the `messages` field. For Twilio, set the sandbox's "when a message
comes in" webhook to the matching `/twilio/webhook` path.

With `WA_PHONE_NUMBER_ID` and `WA_TOKEN` unset the app still runs and answers,
but replies go to an in-memory transport. That is the intended way to exercise
the webhook before onboarding a number.

## Before this faces real users

Deliberate prototype limits, not oversights:

- **Meta Business verification.** Required before a production number can
  message people who have not messaged you first. Days to weeks; start it
  early, it is the longest lead time in the project.
- **Approved template messages.** The app now sends them, and refuses rather
  than pretending when one is missing, but the templates themselves must be
  created and approved in an account nobody here has. Meta review is hours to
  days per template, and a rejected one gives no delivery at all.
- **Phone number is the identity.** That is Cloud API's guarantee for inbound
  messages. A typed number is verified by one-time code under
  `WACAL_REQUIRE_OTP=1`; a *recycled* number still inherits the previous
  owner's calendar, which no code can detect.
- **SQLite behind one process-wide lock, and that lock is the real guard.**
  `events_no_double_book` covers `(owner_id, start_utc)` only, so it catches an
  identical start but not a 09:15-09:45 booking landing inside an existing
  09:00-10:00 one — which variable service durations produce. In one process
  the lock plus the re-check before insert covers that. `uvicorn --workers N`
  would **not** be safe: move to Postgres and replace the index with
  `EXCLUDE USING gist (owner_id WITH =, tstzrange(start_utc, end_utc) WITH &&)`
  first.
- **Google tokens are encrypted at rest** with `WACAL_TOKEN_KEY` (see
  `wacal/crypto.py` and SECURITY.md); without the key the app refuses to store
  one. The database file is still worth treating as a secret: it holds
  everybody's bookings in the clear.
- **Daylight saving.** `block` refuses a time that does not exist on a
  spring-forward day, and on a fall-back day it says which of the two 01:30s
  it took and how to ask for the other. Durations are wall-clock differences,
  so "01:30-02:00" is always 30 minutes.
- **One external calendar per user, Google only.** `calendar_links.provider`
  is read through the registry in `wacal/providers.py`, so an unsupported
  value is an error rather than silently handed to Google. A second provider
  needs four methods registered there, its own OAuth route, and a connect verb.
- **No CORS.** Their reference implementation allows every origin on its admin
  API; that is not reproduced here. Add a specific allowlist if a browser
  dashboard ever needs one.
- **No recurring appointments, no reminders, no payments.** `cancel` then book
  again is the only reschedule path in peer mode; business mode has
  `reschedule`, which is the same two steps behind one word.
