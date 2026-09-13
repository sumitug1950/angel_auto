"""Checks config/config.yaml + config/strategies.yaml and prints the settings the app will
actually use - run it after every config edit, before restarting the app.

Usage:
    .venv\\Scripts\\python.exe scripts\\check_config.py
"""
from __future__ import annotations

import sys

import yaml
from pydantic import ValidationError

from angel_auto.settings import CONFIG_DIR, AppConfig, StrategiesFile, _load_yaml

HINTS = {
    "extra_forbidden": "ye setting pehchani nahi gayi - spelling check karein",
    "missing": "ye setting zaroori hai, file mein nahi mili",
    "literal_error": "ye value allowed nahi",
    "string_pattern_mismatch": "time \"HH:MM\" format mein likhein, jaise \"09:15\"",
    "greater_than": "value isse badi honi chahiye",
    "greater_than_equal": "value isse badi ya barabar honi chahiye",
    "less_than": "value isse chhoti honi chahiye",
}


def _load(model, filename: str):
    path = CONFIG_DIR / filename
    try:
        return model.model_validate(_load_yaml(path))
    except yaml.YAMLError as exc:
        print(f"GALTI {filename}: file ka format (YAML) toota hua hai - spacing/colon check karein\n  {exc}")
    except ValidationError as exc:
        print(f"GALTI {filename}:")
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]) or "(poori file)"
            hint = HINTS.get(err["type"], "")
            print(f"  {where}: {err['msg']}" + (f"  <- {hint}" if hint else ""))
    return None


def main() -> int:
    app = _load(AppConfig, "config.yaml")
    strategies = _load(StrategiesFile, "strategies.yaml")
    if app is None or strategies is None:
        print("\nConfig mein galti hai - theek karke dobara chalayein. App is config ke saath start NAHI hoga.")
        return 1

    s = strategies.active
    print("Config sahi hai. App ye settings use karega:\n")
    print(f"  mode: {app.mode.value}   |   {app.underlying}, lot size {app.lot_size}, har trade {s.sizing.lots} lot")
    print(f"  flagship strategy: {'CHALU' if strategies.flagship_enabled else 'BAND'}")
    for label, rules in (("Buying ", s.buying), ("Selling", s.selling)):
        print(f"  {label}: {rules.expiry} expiry (kam se kam {rules.min_days_to_expiry} din baaki), "
              f"strike gap {rules.strike_grid:g}, ITM delta {rules.itm_delta}, OTM delta {rules.otm_delta}")
    print(f"  shuru mein button: {s.start_with}")
    print(f"  VIX {s.vix_override.threshold_pct}% badhe -> {s.vix_override.on_rise}, "
          f"{s.vix_override.threshold_pct}% ghate -> {s.vix_override.on_fall}")
    print(f"  MACD {s.macd.fast_period}/{s.macd.slow_period}/{s.macd.signal_period} on {s.candle_interval_sec}-sec candles, "
          f"entry se pehle kam se kam {s.macd.min_candles_before_entry} candles")
    print(f"  check har {s.check_interval_sec:g} sec   |   strike range spot se +/-{s.option_band_points:g}")
    print(f"  SL Rs{s.exit.sl_amount_rs:g}   |   target Rs{s.exit.target_amount_rs:g} (phir trailing Rs{s.exit.trail_gap_rs:g})   |   "
          f"MACD ulta hone par exit: {'haan' if s.exit.exit_on_opposite_macd else 'nahi'}")
    backup = (f"haan, Selling trades par (sell leg ka loss Rs{s.exit.sl_amount_rs * s.exit.broker_backup_sl_multiple:g} par)"
              if s.exit.broker_backup_sl else "nahi")
    margin = "haan" if app.mode.value == "live" or app.paper_trading.simulate_margin_check else "nahi (paper)"
    print(f"  broker backup SL: {backup}   |   entry se pehle margin check: {margin}   |   "
          f"fill ka intezaar {app.oms.fill_timeout_sec:g} sec")
    print(f"  din ki limit: max {app.risk.max_trades_per_day} trade, loss Rs{app.risk.daily_loss_limit_rs:g}, "
          f"lagatar {app.risk.max_consecutive_losses} loss par band")
    print(f"  square-off {app.square_off.normal_time} (expiry din {app.square_off.expiry_day_time})")
    print("\nApp restart karein taaki ye settings lag jaayein.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
