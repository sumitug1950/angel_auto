"""Angel One instrument/scrip master - symbol/token lookups.

Downloads and locally caches Angel One's public scrip master (~150k instruments across
all exchanges/segments) and exposes lookups for the NIFTY spot index token and NIFTY
option contract tokens by expiry/strike/option-type. Everything else in the system that
needs a token goes through this module rather than hardcoding one, since tokens/expiries
roll over time and strikes/lot-size can legitimately change (lot size was last confirmed
at 65, not the more commonly quoted historical 50/75 - see config.yaml).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from angel_auto.logging_conf import get_logger

log = get_logger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_PATH = Path("data_store/scrip_master.json")
CACHE_MAX_AGE_SEC = 24 * 60 * 60  # instruments/expiries roll daily-ish; re-download once a day

EXPECTED_NIFTY_SPOT_TOKEN = "26000"  # sanity-check only; the real value always comes from the master


@dataclass(frozen=True)
class Instrument:
    token: str
    symbol: str
    name: str
    expiry: str  # "" for non-derivatives, else e.g. "18AUG2026"
    strike: float  # already divided by the master's implicit x100
    lot_size: int
    instrument_type: str
    exchange: str


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def _download_scrip_master() -> list[dict]:
    log.info("scrip_master_downloading", url=SCRIP_MASTER_URL)
    resp = requests.get(SCRIP_MASTER_URL, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _cache_is_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    return (time.time() - CACHE_PATH.stat().st_mtime) < CACHE_MAX_AGE_SEC


class InstrumentMaster:
    """Load once (downloads/caches as needed), query many times."""

    def __init__(self) -> None:
        self._by_name_type_expiry: dict[tuple[str, str, str], list[Instrument]] = {}
        self._loaded = False

    def load(self, force_refresh: bool = False) -> None:
        if not force_refresh and _cache_is_fresh():
            log.info("scrip_master_cache_hit", path=str(CACHE_PATH))
            raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        else:
            raw = _download_scrip_master()
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(raw), encoding="utf-8")
            log.info("scrip_master_cached", path=str(CACHE_PATH), count=len(raw))

        by_key: dict[tuple[str, str, str], list[Instrument]] = {}
        for rec in raw:
            try:
                strike_raw = float(rec.get("strike") or 0)
            except ValueError:
                strike_raw = 0.0
            instrument = Instrument(
                token=rec["token"],
                symbol=rec["symbol"],
                name=rec.get("name", ""),
                expiry=rec.get("expiry", ""),
                strike=strike_raw / 100.0,
                lot_size=int(rec.get("lotsize") or 1),
                instrument_type=rec.get("instrumenttype", ""),
                exchange=rec.get("exch_seg", ""),
            )
            key = (instrument.name, instrument.instrument_type, instrument.expiry)
            by_key.setdefault(key, []).append(instrument)

        self._by_name_type_expiry = by_key
        self._loaded = True
        log.info("scrip_master_loaded", instrument_count=len(raw))

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def nifty_spot_instrument(self) -> Instrument:
        """The NSE_CM 'NIFTY' index token used for live spot feed subscription."""
        self._ensure_loaded()
        candidates = [
            inst
            for inst in self._by_name_type_expiry.get(("NIFTY", "", ""), [])
            if inst.exchange == "NSE" and inst.symbol == "NIFTY"
        ]
        if not candidates:
            raise LookupError("NIFTY spot instrument not found in scrip master")
        instrument = candidates[0]
        if instrument.token != EXPECTED_NIFTY_SPOT_TOKEN:
            log.warning(
                "nifty_spot_token_mismatch",
                expected=EXPECTED_NIFTY_SPOT_TOKEN,
                actual=instrument.token,
            )
        return instrument

    def option_chain(self, name: str, expiry: str) -> list[Instrument]:
        """All OPTIDX contracts for `name` (e.g. 'NIFTY') at the given expiry (e.g. '18AUG2026')."""
        self._ensure_loaded()
        return list(self._by_name_type_expiry.get((name, "OPTIDX", expiry), []))

    def available_expiries(self, name: str) -> list[str]:
        self._ensure_loaded()
        expiries = {key[2] for key in self._by_name_type_expiry if key[0] == name and key[1] == "OPTIDX"}
        return sorted(expiries, key=lambda e: datetime.strptime(e, "%d%b%Y"))

    def nearest_weekly_expiry(self, name: str, as_of: date | None = None) -> str:
        as_of = as_of or date.today()
        for expiry in self.available_expiries(name):
            if datetime.strptime(expiry, "%d%b%Y").date() >= as_of:
                return expiry
        raise LookupError(f"No future expiry found for {name}")

    def find_option(
        self, name: str, expiry: str, strike: float, option_type: Literal["CE", "PE"]
    ) -> Instrument | None:
        for inst in self.option_chain(name, expiry):
            if inst.symbol.endswith(option_type) and abs(inst.strike - strike) < 0.01:
                return inst
        return None

    def strikes_for_expiry(self, name: str, expiry: str) -> list[float]:
        strikes = {inst.strike for inst in self.option_chain(name, expiry)}
        return sorted(strikes)
