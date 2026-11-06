"""Load .env, then verify the account or serve the app.

The demo machine has no secret vault, so .env is the source of configuration.
Everything that needs it goes through here rather than each script growing its
own loader.

    python deploy/run.py --check                 # read-only: does the token work?
    python deploy/run.py --serve [--port 8090]   # migrate, seed, serve
    python deploy/run.py --send +9198...         # one real template message

--check and --send talk to Meta; --check only reads.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> int:
    """Minimal .env reader. A variable already in the environment wins."""
    if not path.exists():
        raise SystemExit(f"{path} is missing -- run deploy/bootstrap.ps1 first")
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def check() -> int:
    """Prove the token and ids before anything is built on them."""
    waba = os.environ.get("WA_WABA_ID", "").strip()
    sys.argv = [str(REPO / "meta_ids.py")] + (["--waba", waba] if waba else [])
    sys.path.insert(0, str(REPO))
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def serve(port: int) -> int:
    sys.argv = [str(REPO / "start.py"), "--port", str(port), "--no-demo"]
    os.chdir(REPO)
    sys.path.insert(0, str(REPO))
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def send(to: str) -> int:
    """One hello_world template message, through wacal's own transport."""
    import asyncio

    sys.path.insert(0, str(REPO))
    from wacal import templates
    from wacal.whatsapp import CloudTransport, Reply

    phone_number_id = os.environ["WA_PHONE_NUMBER_ID"]
    os.environ["WA_TEMPLATE_BOOKING_CONFIRMED"] = "hello_world"
    reply = Reply(text="(template)",
                  template=templates.build(templates.BOOKING_CONFIRMED))
    transport = CloudTransport(phone_number_id, os.environ["WA_TOKEN"])
    result = asyncio.run(transport.send(to, reply, force_template=True))
    print(result)
    return 0


def main(argv: list[str]) -> int:
    print(f"loaded {load_env(REPO / '.env')} variable(s) from .env")
    if "--check" in argv:
        return check()
    if "--serve" in argv:
        port = 8090
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        return serve(port)
    if "--send" in argv:
        return send(argv[argv.index("--send") + 1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
