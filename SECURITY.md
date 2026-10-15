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

## Still outstanding

- Google refresh tokens are stored as plain JSON in `calendar_links`. Encrypt
  at rest before production, and treat the database file as a secret.
- No rate limiting. A valid signature can be replayed as volume.
- Single process, single SQLite lock. The double-booking guard is that lock
  plus a re-check plus a partial unique index on `(owner_id, start_utc)`; the
  index alone does not catch overlapping events with different start times, so
  multiple workers would need a Postgres range exclusion constraint instead.

Report anything found here by opening an issue.
