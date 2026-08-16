from datetime import date

from angel_auto.data.instruments import Instrument, InstrumentMaster


def _fake_master(expiries: list[str]) -> InstrumentMaster:
    """Builds an InstrumentMaster with synthetic NIFTY OPTIDX entries at the given expiries,
    bypassing the network download - keeps expiry-selection tests deterministic regardless
    of what "today" actually is.
    """
    master = InstrumentMaster()
    by_key: dict[tuple[str, str, str], list[Instrument]] = {}
    for i, expiry in enumerate(expiries):
        inst = Instrument(
            token=str(1000 + i),
            symbol=f"NIFTY{expiry}24800CE",
            name="NIFTY",
            expiry=expiry,
            strike=24800.0,
            lot_size=65,
            instrument_type="OPTIDX",
            exchange="NFO",
        )
        by_key[("NIFTY", "OPTIDX", expiry)] = [inst]
    master._by_name_type_expiry = by_key
    master._loaded = True
    return master


def test_monthly_expiries_picks_last_expiry_per_calendar_month():
    # 2 weeklies in Aug, 3 weeklies in Sep (last one 29SEP is the Sep monthly), 1 in Oct
    master = _fake_master(
        ["18AUG2026", "25AUG2026", "01SEP2026", "08SEP2026", "29SEP2026", "27OCT2026"]
    )
    assert master.monthly_expiries("NIFTY") == ["25AUG2026", "29SEP2026", "27OCT2026"]


def test_select_monthly_expiry_uses_nearest_when_gap_is_sufficient():
    master = _fake_master(["25AUG2026", "29SEP2026", "27OCT2026"])
    # as_of well before 25AUG - plenty of gap
    selected = master.select_monthly_expiry("NIFTY", min_days_gap=10, as_of=date(2026, 8, 1))
    assert selected == "25AUG2026"


def test_select_monthly_expiry_rolls_forward_when_nearest_is_too_close():
    master = _fake_master(["25AUG2026", "29SEP2026", "27OCT2026"])
    # as_of = 2026-08-16 -> 25AUG2026 is only 9 days away, below the 10-day minimum
    selected = master.select_monthly_expiry("NIFTY", min_days_gap=10, as_of=date(2026, 8, 16))
    assert selected == "29SEP2026"


def test_select_monthly_expiry_boundary_exactly_min_gap_is_accepted():
    master = _fake_master(["25AUG2026", "29SEP2026"])
    # exactly 10 days away - should be accepted (>=, not >)
    selected = master.select_monthly_expiry("NIFTY", min_days_gap=10, as_of=date(2026, 8, 15))
    assert selected == "25AUG2026"


def test_select_monthly_expiry_raises_when_nothing_far_enough_out():
    master = _fake_master(["25AUG2026"])
    try:
        master.select_monthly_expiry("NIFTY", min_days_gap=10, as_of=date(2026, 8, 20))
        assert False, "expected LookupError"
    except LookupError:
        pass
