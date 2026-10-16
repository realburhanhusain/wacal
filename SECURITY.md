# Security notes

This is a prototype, but the parts below are deliberate and should not be
loosened without replacing them with something equivalent.

## Webhooks fail closed

Both transports' routes are always mounted, so a Meta-only deployment still
exposes `/twilio/webhook`. If that transport's signing secret is unset, the
route answers **403** rather than accepting the request.

Earlier it returned "valid" when the secret was empty. That made the unused
transport an unauthenticated command channel: a `POST` with
`From=<victim>&Body=share <attacker>` and no signature was believed, and
granted the attacker the victim's calendar.

- Cloud API needs `WA_APP_SECRET`.
- Twilio needs `TWILIO_AUTH_TOKEN`.
- `WACAL_ALLOW_UNSIGNED=1` disables both checks, for local work only.

`TWILIO_PUBLIC_URL` is a public **origin**, not a full URL: Twilio signs the
exact URL configured per webhook, so substituting the whole string made every
path except one fail verification.

## OAuth state is signed and single use

`connect google` hands out a link whose `state` is an HMAC over the user id, an
issue time and a random nonce, valid 15 minutes. The nonce is stored in
`oauth_states` and spent by the callback.

- There is **no default signing key**. With one, the key would be published in
  this repository, and anyone could mint a state for any user id, consent with
  their own Google account, and have it saved against that user — taking over
  whose calendar the app reads and writes.
- A signed-but-never-issued state has no nonce row, so it cannot be spent.
- A replayed state fails on the second use.

## Identity

The sender's phone number is the identity, normalised to `+<digits>` so the
same person is one row on either transport. That is the provider's guarantee
for inbound messages, and it is the weakest link here: there is no second
factor, so a recycled number inherits the previous owner's calendar. Add a
one-time code at registration before trusting this with anything sensitive.

## What is not exposed

- Free/busy returns intervals only. Event titles never reach a third party.
  `GET /dashboard/{slug}/calendar` does show titles, but only the owner's own
  calendar, behind the admin key.
- `GET /webhook/{slug}/status` is unauthenticated and returns only
  `{"slug", "active"}` — no business name, timezone or calendar state.
- `/dashboard/*` answers 503 when `ADMIN_KEY` is unset, never open.
- Verify-token and API-key comparisons use `hmac.compare_digest`.

## Calendar tokens are encrypted at rest

A Google refresh token is a standing grant to read and write someone's
calendar. They are stored as `enc:v1:<fernet token>` using `WACAL_TOKEN_KEY`
(`python -m wacal.crypto` prints a key).

- Storing a token with no key configured is **refused**, not silently done in
  the clear. `WACAL_ALLOW_PLAINTEXT_TOKENS=1` overrides that for local work.
- Rows written before encryption existed still read, and re-encrypt on their
  next save.
- A key that no longer matches raises rather than returning nonsense; affected
  users must send `connect google` again.
- Read tokens through `db.token_of(link)`, never `link["token_json"]`.

## One writer process per database

The double-booking guard is an in-process lock plus a re-check plus a partial
unique index on `(owner_id, start_utc)`. The index alone does not catch an
overlapping booking with a different start time, which variable service
durations produce routinely — so `uvicorn --workers 4` is not slower, it is
wrong.

Startup therefore takes an exclusive advisory lock on
`<database>.writer.lock`, and a second process fails loudly. The holder's pid
is written to `<database>.writer.pid` for whoever has to look. Set
`WACAL_ALLOW_MULTIPROCESS=1` only once writes are guarded across processes,
e.g. by a Postgres `EXCLUDE USING gist` constraint.

## Rate limiting

A valid signature is not a promise of good behaviour, and every message costs a
booking pass plus possibly a Google round trip, all behind one process-wide
lock — so one sender can stall everyone.

Two sliding windows per sender, a burst and a sustained one, both configurable
and generous by default. A throttled sender gets **one** explanation and is
then dropped silently, because replying to every message of a flood is itself
a flood. Throttled messages are still marked seen so the provider stops
redelivering them, and one sender's block never affects another's.

In-memory state, which is correct because the app refuses to run as more than
one process.

## One-time codes

An inbound WhatsApp message proves its sender: Meta signs the webhook and the
number is observed. A number somebody *types* into
`POST /dashboard/register` proves nothing, and that number then receives other
people's booking details — so a typo there is a disclosure. Under
`WACAL_REQUIRE_OTP=1` registration refuses a number that has not answered a
code.

The code is never stored. `otp_codes` holds `HMAC-SHA256(pepper, phone:code)`,
keyed with `WACAL_OTP_PEPPER` or `WACAL_TOKEN_KEY` and **bound to the phone**,
so a row copied onto another number verifies nothing there. Six digits is
weak on its own, so the cap is the control: ten-minute expiry, five attempts
before the challenge is destroyed, one resend a minute, and single use —
verifying consumes the challenge in the same transaction that records the
proof, so two concurrent verifications cannot both succeed. `tests/
mutation_check.py` has mutants for an unlimited and a never-expiring code;
both are killed.

## Messages outside the 24-hour window fail visibly

WhatsApp drops a free-form message sent more than 24 hours after the
recipient's last inbound one. Before this, wacal sent those anyway and
recorded them as delivered, so an owner could simply never learn about a
confirmed booking.

Now the drainer checks `contact_window` for the recipient and either sends an
approved template or **refuses**: the outbox row stays pending with the reason
in `last_error`, retried on every tick and delivered the moment that person
next messages the number. The local escape hatch
(`WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW=1`) is deliberate and loud — setting it
in production turns a visible refusal back into an invisible drop. A number we
have never heard from counts as *outside* the window, not inside it.

## Still outstanding

- Nothing from the two reviews. See the README for the deliberate product
  limits that remain (Meta Business verification and template approval,
  payments, reminders).

Report anything found here by opening an issue.
