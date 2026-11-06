# Running the wacal demo on a spare machine

Everything here is for one purpose: making a WhatsApp message you actually
type reach wacal and get a real reply. Outbound already works from anywhere;
inbound needs a public HTTPS URL, which needs a machine without a corporate
proxy.

**Requirement: the machine must NOT be running the Netskope client.** It
intercepts TLS locally, so it closes cloudflared's port 7844 and kills ngrok's
session after the handshake, on any network — switching to a phone hotspot
does not help, because the agent is on the laptop, not the network. Check with
`Get-Service stAgentSvc`: "not found" is what you want.

## What is already true

Verified live against the Meta test number, nothing to redo:

| | |
| --- | --- |
| app | `business - Test1`, id `1477783504400676` |
| WABA | `2232461004084313` |
| phone number id | `1387507791118752` → **+1 555-197-7042**, quality GREEN |
| token | system user `burhan`, type `SYSTEM_USER`, **expires never** |
| allowed recipient | **+91 8095786110** (a test number reaches at most 5 verified numbers) |

Already proven end to end: a real template message delivered to +918095786110
through wacal's own `CloudTransport`; a signed webhook accepted (200) with
unsigned and tampered copies both refused (401). The only untested path is
Meta → wacal over the public internet.

## Steps

Two windows. Both from the repo root.

### 1. Serve

```powershell
git clone https://github.com/realburhanhusain/wacal
cd wacal
pwsh -NoProfile -File deploy\bootstrap.ps1
```

It installs dependencies, asks once for the two secrets (typed hidden, written
only to the gitignored `.env`), verifies the token against the live account,
then serves on port 8090. Leave it running.

The two secrets:

1. **System-user token** — Business settings → Users → System users → `burhan`.
   A token is displayed once, so if you do not still have it, generate a new
   one: scopes `whatsapp_business_messaging` + `whatsapp_business_management`,
   expiry **Never**. The old one keeps working; there is no need to revoke it.
2. **App secret** — Meta app → Settings → Basic → App secret → Show.

### 2. Tunnel

Second window:

```powershell
pwsh -NoProfile -File deploy\tunnel.ps1
```

Prints `https://<words>.trycloudflare.com`. Leave it open — closing it
destroys the URL. If cloudflared cannot reach port 7844, that machine also has
a proxy in the way; the fallback is `ngrok http 8090` (the authtoken is in the
vault on the work laptop).

### 3. Point Meta at it

```powershell
python deploy\meta_webhook.py                                  # read, change nothing
python deploy\meta_webhook.py --apply https://<words>.trycloudflare.com
```

`--apply` does the two registrations that both have to exist, and which the
dashboard keeps in different places:

- the **app's** webhook for `whatsapp_business_account` — callback URL and
  verify token. Meta immediately GETs the URL with `hub.challenge`; wacal
  answers it, so a failure here means the tunnel is not reaching the app.
- the **WABA's** subscription to the app. Without it there is a callback URL
  that is never called, which looks exactly like a broken tunnel.

Measured 2026-11-06: the WABA subscription **already exists** (`business -
Test1`, alongside Meta's own `WA DevX Webhook Events 1P App`), and the app
webhook is `NOT SET`. So in practice only the callback URL is outstanding, and
re-subscribing is harmless — the call is idempotent.

Pass the origin only; `/webhook` is appended. Re-run with a new URL whenever
the tunnel restarts — the URL changes every time.

### 4. Text it

From +91 8095786110, message **+1 555-197-7042**: `hi`, then `help`.

Expect the command menu back. Then try the peer flow: `free <number> today`
returns tappable slots, and tapping one books it.

A reply arriving proves the loop. If nothing comes back, in order: the serving
window logs every POST and its status; `python deploy\meta_webhook.py` shows
whether both registrations survived; 401 means the app secret in `.env` does
not match the app.

## Sending one message without any of this

Outbound needs no tunnel:

```powershell
python deploy\run.py --send +918095786110
```

## What is deliberately not done

- **The three templates do not exist yet.** The account holds only Meta's
  samples (`hello_world`, `jaspers_market_*`). Until they are approved, any
  notification to someone outside their 24-hour window is **held in the outbox
  with the reason in `last_error`**, not silently dropped, and is delivered the
  moment that person next messages the number. That is intended behaviour, not
  a fault. Create them in WhatsApp Manager → Message templates, then fill in
  `WA_TEMPLATE_*` in `.env`:

  | Template | Category | `{{1}}` | `{{2}}` | `{{3}}` |
  | --- | --- | --- | --- | --- |
  | `wacal_booking_confirmed` | Utility | who booked what | when, with timezone | ref |
  | `wacal_booking_cancelled` | Utility | who cancelled | when, with timezone | ref |
  | `wacal_otp` | Authentication | the code | — | — |

  A body such as `New booking: {{1}}` / `When: {{2}}` / `Ref: {{3}}`. On the
  authentication one, tick the copy-code button; wacal passes the code to it
  as well as to the body.

- **Not always-on.** A quick tunnel lives as long as its window. While the
  machine is off, Meta's POSTs fail and it retries for a while, so a message
  sent during downtime may be lost. For a demo that is fine. Making it
  permanent means a host that stays up — which is a separate decision, since
  free-tier hosting either costs a few dollars (Fly, a VPS) or sleeps and
  loses the database (Render's free tier).

- **Business verification.** Not needed for any of the above. It is what would
  let the number message people who are not on the 5-recipient test list.

## Secrets

`.env` is gitignored and holds the only copy on this machine. The record copies
live in the vault on the work laptop (`wacal-meta-wa-token`,
`wacal-meta-app-secret`, `ngrok-authtoken`). Both values have passed through a
chat transcript, so treat them as exposed: fine for a test number with five
allowed recipients, rotate before anything real depends on them.
