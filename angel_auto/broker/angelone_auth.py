"""Angel One SmartAPI login (TOTP) and session/token lifecycle.

This module only handles authentication - order placement lives in angelone_rest.py,
tick streaming in angelone_ws.py. Keeping login isolated makes it easy to unit-test and
to call from a scheduler job for the daily re-login.
"""
from __future__ import annotations

from dataclasses import dataclass

import pyotp
from SmartApi import SmartConnect
from tenacity import retry, stop_after_attempt, wait_exponential

from angel_auto.logging_conf import get_logger
from angel_auto.settings import BrokerCredentials

log = get_logger(__name__)


class AngelOneAuthError(RuntimeError):
    """Raised on any login/session failure - never contains the raw secret values."""


@dataclass
class AngelSession:
    smart_connect: SmartConnect
    jwt_token: str
    refresh_token: str
    feed_token: str
    client_code: str


def _generate_totp(totp_secret: str) -> str:
    return pyotp.TOTP(totp_secret).now()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def login(credentials: BrokerCredentials) -> AngelSession:
    """Log in with TOTP and return a live SmartConnect session.

    Retries transient failures (network blips) up to 3 times. A rejected login
    (bad credentials/TOTP) still raises immediately after the SmartAPI call returns -
    tenacity only kicks in on exceptions, not on a clean "status: false" response after
    the first attempt, since retrying bad credentials is pointless.
    """
    missing = [
        name
        for name, value in [
            ("ANGEL_API_KEY", credentials.api_key),
            ("ANGEL_CLIENT_CODE", credentials.client_code),
            ("ANGEL_MPIN", credentials.mpin),
            ("ANGEL_TOTP_SECRET", credentials.totp_secret),
        ]
        if not value
    ]
    if missing:
        raise AngelOneAuthError(f"Missing credentials in config/.env: {', '.join(missing)}")

    totp_code = _generate_totp(credentials.totp_secret)

    smart_connect = SmartConnect(api_key=credentials.api_key)
    session_data = smart_connect.generateSession(credentials.client_code, credentials.mpin, totp_code)

    if not session_data or not session_data.get("status"):
        message = (session_data or {}).get("message", "unknown error")
        log.error("angelone_login_failed", client_code=credentials.client_code, reason=message)
        raise AngelOneAuthError(f"Angel One login rejected: {message}")

    data = session_data["data"]
    feed_token = smart_connect.getfeedToken()

    log.info("angelone_login_success", client_code=credentials.client_code)

    return AngelSession(
        smart_connect=smart_connect,
        jwt_token=data["jwtToken"],
        refresh_token=data["refreshToken"],
        feed_token=feed_token,
        client_code=credentials.client_code,
    )


def renew_session(session: AngelSession) -> AngelSession:
    """Refresh the JWT on an existing SmartConnect instance using its stored refresh token."""
    result = session.smart_connect.renewAccessToken()
    if not result or not result.get("status"):
        message = (result or {}).get("message", "unknown error")
        log.error("angelone_renew_failed", client_code=session.client_code, reason=message)
        raise AngelOneAuthError(f"Angel One token renewal failed: {message}")

    data = result["data"]
    session.jwt_token = data["jwtToken"]
    session.refresh_token = data["refreshToken"]
    session.feed_token = session.smart_connect.getfeedToken()

    log.info("angelone_renew_success", client_code=session.client_code)
    return session


def logout(session: AngelSession) -> None:
    try:
        session.smart_connect.terminateSession(session.client_code)
        log.info("angelone_logout_success", client_code=session.client_code)
    except Exception as exc:  # noqa: BLE001 - logout is best-effort, never let it crash the caller
        log.warning("angelone_logout_failed", client_code=session.client_code, error=str(exc))
