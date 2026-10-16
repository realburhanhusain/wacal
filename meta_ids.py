"""Discover the Meta ids wacal needs, from nothing but an access token.

The Cloud API wants four values and the dashboard shows them in four
different places. Every one of them is derivable from the token itself, so
paste the token and read the answer:

    python meta_ids.py                 # reads WA_TOKEN
    python meta_ids.py <token>
    python meta_ids.py --env           # print as a .env block

What it reports:

  app id              from debug_token -- which app issued this token
  token type          USER, PAGE or SYSTEM_USER. A system user token is the
                      one to deploy with; a user token from the dashboard's
                      "temporary access token" box expires in 24 hours
  expires             a deployment on a token that expires is a deployment
                      that stops working, silently, on a Tuesday
  scopes              whatsapp_business_messaging is required to send, and
                      whatsapp_business_management to read the account
  WABA id             the WhatsApp Business Account, from the token's own
                      granular scopes, falling back to the business
  phone number id     per WABA. This is the id in the send URL -- NOT the
                      phone number, and not the WABA id
  templates           name, language and status per WABA, so an approved
                      name can be copied straight into WA_TEMPLATE_*

Nothing here writes. The token is never printed back, including in errors.
"""

from __future__ import annotations

import os
import sys

import httpx

GRAPH = "https://graph.facebook.com"
VERSION = os.environ.get("WA_GRAPH_VERSION", "v21.0")
# Scopes a token must carry to be usable for each half of the job.
NEEDED = {
    "whatsapp_business_messaging": "send messages",
    "whatsapp_business_management": "read the account, phone numbers and "
                                    "templates",
}


class MetaError(RuntimeError):
    pass


def get(path: str, token: str, **params) -> dict:
    url = f"{GRAPH}/{VERSION}/{path.lstrip('/')}"
    response = httpx.get(url, params={"access_token": token, **params},
                         timeout=30)
    if response.status_code >= 400:
        detail = response.json().get("error", {}) if response.content else {}
        raise MetaError(
            f"{response.status_code} on /{path.lstrip('/')}: "
            f"{detail.get('message', response.text[:200])}"
        )
    return response.json()


def debug(token: str) -> dict:
    """What the token is. input_token and access_token are the same token.

    Using the token to inspect itself works for a user or system user token
    and avoids needing the app secret, which should not be on a laptop.
    """
    return get("debug_token", token, input_token=token).get("data", {})


def waba_ids(info: dict, token: str) -> list[str]:
    """Every WhatsApp Business Account this token can act on.

    granular_scopes is the reliable source: it lists the exact ids the token
    was granted against, which is what the send call will be checked against.
    The business traversal below is the fallback for a token whose scopes
    carry no target ids.
    """
    found: list[str] = []
    for scope in info.get("granular_scopes", []) or []:
        if scope.get("scope") in ("whatsapp_business_messaging",
                                  "whatsapp_business_management"):
            for target in scope.get("target_ids", []) or []:
                if target not in found:
                    found.append(target)
    if found:
        return found

    try:
        businesses = get("me/businesses", token).get("data", [])
    except MetaError:
        return []
    for business in businesses:
        try:
            owned = get(f"{business['id']}/owned_whatsapp_business_accounts",
                        token).get("data", [])
        except MetaError:
            continue
        found.extend(account["id"] for account in owned)
    return found


def phone_numbers(waba_id: str, token: str) -> list[dict]:
    return get(f"{waba_id}/phone_numbers", token,
               fields="id,display_phone_number,verified_name,"
                      "quality_rating,code_verification_status").get("data", [])


def message_templates(waba_id: str, token: str) -> list[dict]:
    return get(f"{waba_id}/message_templates", token,
               fields="name,status,category,language", limit=50).get("data", [])


def report(token: str, as_env: bool = False) -> int:
    info = debug(token)
    if not info.get("is_valid"):
        print("This token is not valid. Generate a new one in the Meta app "
              "dashboard under WhatsApp > API Setup.")
        return 1

    app_id = str(info.get("app_id", ""))
    expires = info.get("expires_at", 0)
    scopes = info.get("scopes", []) or []
    accounts = waba_ids(info, token)

    lines: list[str] = []
    if not as_env:
        print(f"app id         {app_id}")
        print(f"app name       {info.get('application', '?')}")
        print(f"token type     {info.get('type', '?')}")
        print("expires        "
              + ("never (system user token)" if expires == 0
                 else f"{expires} (unix) -- a deploy on this stops working"))
        missing = [scope for scope in NEEDED if scope not in scopes]
        print(f"scopes         {', '.join(scopes) or '(none)'}")
        for scope in missing:
            print(f"  MISSING {scope} -- needed to {NEEDED[scope]}")
        if not accounts:
            print("\nNo WhatsApp Business Account is reachable with this "
                  "token. In the app dashboard, add the WhatsApp product, "
                  "then re-generate the token with whatsapp_business_"
                  "management granted.")
            return 1

    for waba_id in accounts:
        if not as_env:
            print(f"\nWABA id        {waba_id}")
        for number in phone_numbers(waba_id, token):
            if as_env:
                lines.append(f"WA_PHONE_NUMBER_ID={number['id']}")
            else:
                print(f"  phone number id  {number['id']}")
                print(f"    number         {number.get('display_phone_number')}"
                      f"  ({number.get('verified_name', '')})")
                print(f"    quality        "
                      f"{number.get('quality_rating', 'n/a')}")
        if not as_env:
            try:
                approved = message_templates(waba_id, token)
            except MetaError as exc:
                print(f"  templates        unreadable: {exc}")
                continue
            if not approved:
                print("  templates        none yet. Create them under "
                      "WhatsApp Manager > Message templates.")
            for template in approved:
                print(f"  template         {template['name']} "
                      f"[{template.get('language')}] "
                      f"{template.get('status')} "
                      f"({template.get('category')})")

    if as_env:
        lines.insert(0, f"# generated by meta_ids.py for app {app_id}")
        lines.append("WA_TRANSPORT=cloud")
        lines.append("# WA_TOKEN=<your token -- not printed here>")
        print("\n".join(lines))
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--env"]
    as_env = "--env" in argv[1:]
    token = (args[0] if args else os.environ.get("WA_TOKEN", "")).strip()
    if not token:
        print(__doc__)
        print("No token given and WA_TOKEN is not set.")
        return 2
    try:
        return report(token, as_env=as_env)
    except MetaError as exc:
        # The token is deliberately absent from this message.
        print(f"Meta refused the request: {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"Could not reach {GRAPH}: {exc.__class__.__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
