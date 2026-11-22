"""One-time interactive Garmin Connect login for this machine.

Run it yourself in a terminal (it prompts for credentials and an MFA code):

    .venv/bin/python garmin_login.py

On success the token is cached in the tokenstore named by config.json
(`garmin.tokenstore`, default ~/.garminconnect) and `sync` can read Garmin's
dog tags unattended until the token expires — typically months. Rerun this
when sync starts saying Garmin is unavailable. Credentials are never stored:
only the resulting token is written.

Giving this machine its own login — rather than copying a token from another
machine — keeps the two independent, so one refreshing its token doesn't
invalidate the other.
"""
import getpass
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main():
    cfg = json.loads((BASE / "config.json").read_text())
    store = os.path.expanduser((cfg.get("garmin") or {}).get("tokenstore") or "~/.garminconnect")
    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit("garminconnect isn't installed here. Try: .venv/bin/pip install garminconnect")

    try:
        Garmin().login(store)
        print(f"Already logged in — the cached token at {store} still works.")
        return
    except Exception:
        pass

    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    g = Garmin(email=email, password=password,
               prompt_mfa=lambda: input("MFA code (blank if none): ").strip())
    g.login(store)
    os.chmod(store, 0o700)
    print(f"Logged in. Token cached at {store} — sync can now read Garmin's dog tags.")


if __name__ == "__main__":
    main()
