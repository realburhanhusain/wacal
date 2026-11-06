"""Read -- and optionally set -- the Meta webhook wiring for this app.

Two separate registrations have to exist before an inbound WhatsApp message
reaches wacal, and the dashboard presents them in different places:

  1. The APP's webhook for the whatsapp_business_account object: the callback
     URL and verify token. Written with an app access token (app_id|secret).
  2. The WABA's subscription to this app. Without it the app has a callback
     URL that is never called, which looks identical to a broken tunnel.

    python deploy/meta_webhook.py                      # read both, change nothing
    python deploy/meta_webhook.py --apply <public-url>  # point them at that URL

<public-url> is the tunnel origin, e.g. https://abc-def.trycloudflare.com --
the /webhook path is appended here, so do not include it.

Reads these from the environment (deploy/bootstrap.ps1 sets them from .env):
WA_APP_ID, WA_APP_SECRET, WA_TOKEN, WA_WABA_ID, WA_VERIFY_TOKEN.

--apply writes to your Meta app configuration. It prints exactly what it is
about to change first, and a callback URL is reversible: run it again with a
different URL, or clear it in the dashboard.
"""

from __future__ import annotations

import os
import sys

import httpx

GRAPH = "https://graph.facebook.com"
VERSION = os.environ.get("WA_GRAPH_VERSION", "v21.0")
OBJECT = "whatsapp_business_account"
# The only field wacal needs. Subscribing to everything means every status
# callback (sent/delivered/read) is also POSTed, which wacal ignores but still
# has to verify and parse.
FIELDS = "messages"


class MetaError(RuntimeError):
    pass


def call(method: str, path: str, token: str, **params) -> dict:
    url = f"{GRAPH}/{VERSION}/{path.lstrip('/')}"
    request = httpx.get if method == "GET" else httpx.post
    key = "params" if method == "GET" else "data"
    response = request(url, timeout=30,
                       **{key: {"access_token": token, **params}})
    if response.status_code >= 400:
        detail = response.json().get("error", {}) if response.content else {}
        raise MetaError(f"{response.status_code} on {method} /{path}: "
                        f"{detail.get('message', response.text[:200])}")
    return response.json()


def need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set -- see deploy/DEMO.md")
    return value


def show(app_id: str, app_token: str, waba_id: str, user_token: str) -> None:
    try:
        subs = call("GET", f"{app_id}/subscriptions", app_token).get("data", [])
    except MetaError as exc:
        print(f"app subscriptions unreadable: {exc}")
        subs = []
    whatsapp = [s for s in subs if s.get("object") == OBJECT]
    if not whatsapp:
        print("app webhook        NOT SET for whatsapp_business_account")
    for sub in whatsapp:
        print(f"app webhook        {sub.get('callback_url')}")
        print(f"  active           {sub.get('active')}")
        print("  fields           "
              + ", ".join(f.get("name", "") for f in sub.get("fields", [])))

    try:
        apps = call("GET", f"{waba_id}/subscribed_apps",
                    user_token).get("data", [])
    except MetaError as exc:
        print(f"WABA subscription unreadable: {exc}")
        return
    if not apps:
        print("WABA subscription  NONE -- the app will never be called")
    for app in apps:
        info = app.get("whatsapp_business_api_data", {})
        print(f"WABA subscription  {info.get('name')} (id {info.get('id')})")


def apply(base: str, app_id: str, app_token: str, waba_id: str,
          user_token: str, verify_token: str) -> int:
    base = base.rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("Meta requires https for a callback URL")
    if base.endswith("/webhook"):
        base = base[: -len("/webhook")]
    callback = f"{base}/webhook"

    print(f"setting app {app_id} webhook -> {callback}")
    print(f"  object {OBJECT}, fields {FIELDS}")
    call("POST", f"{app_id}/subscriptions", app_token, object=OBJECT,
         callback_url=callback, verify_token=verify_token, fields=FIELDS)
    print("  done (Meta GET the URL with hub.challenge and wacal answered it)")

    print(f"subscribing WABA {waba_id} to the app")
    call("POST", f"{waba_id}/subscribed_apps", user_token)
    print("  done")

    print()
    show(app_id, app_token, waba_id, user_token)
    return 0


def main(argv: list[str]) -> int:
    app_id = need("WA_APP_ID")
    app_secret = need("WA_APP_SECRET")
    waba_id = need("WA_WABA_ID")
    user_token = need("WA_TOKEN")
    # An app access token is the id and secret joined by a pipe. It is not a
    # user token and cannot send messages; it only edits app configuration.
    app_token = f"{app_id}|{app_secret}"

    if "--apply" in argv:
        index = argv.index("--apply")
        if index + 1 >= len(argv):
            raise SystemExit("--apply needs the public base URL")
        return apply(argv[index + 1], app_id, app_token, waba_id, user_token,
                     need("WA_VERIFY_TOKEN"))

    show(app_id, app_token, waba_id, user_token)
    print("\nNothing was changed. Re-run with --apply <public-url> to set it.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except MetaError as exc:
        print(f"Meta refused the request: {exc}")
        sys.exit(1)
