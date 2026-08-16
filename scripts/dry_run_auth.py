"""Standalone check: can we log in to Angel One with the credentials in config/.env?

Usage:
    .venv\\Scripts\\python.exe scripts\\dry_run_auth.py

Places no orders, subscribes to no feeds - just proves TOTP login works end to end.
"""
from __future__ import annotations

import sys

from angel_auto.broker.angelone_auth import AngelOneAuthError, login, logout
from angel_auto.logging_conf import configure_logging, get_logger
from angel_auto.settings import get_settings

log = get_logger("dry_run_auth")


def main() -> int:
    configure_logging()
    settings = get_settings()

    print(f"Logging in as client code: {settings.credentials.client_code!r} ...")
    try:
        session = login(settings.credentials)
    except AngelOneAuthError as exc:
        print(f"LOGIN FAILED: {exc}")
        return 1

    print("Login OK.")
    print(f"  jwt_token:     {session.jwt_token[:12]}...(truncated)")
    print(f"  refresh_token: {session.refresh_token[:12]}...(truncated)")
    print(f"  feed_token:    {session.feed_token[:12]}...(truncated)")

    try:
        profile = session.smart_connect.getProfile(session.refresh_token)
        if profile.get("status"):
            data = profile.get("data", {})
            print(f"  profile name:  {data.get('name')}")
            print(f"  exchanges:     {data.get('exchanges')}")
        else:
            print(f"  profile fetch returned non-ok status: {profile.get('message')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  profile fetch failed (login itself still succeeded): {exc}")

    logout(session)
    print("Logged out cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
