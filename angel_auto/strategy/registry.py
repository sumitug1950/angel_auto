"""Name -> class map, config-driven (config/strategies.yaml active_strategy + class_path).
Adding a future strategy variant is one new file + one entry here - no engine changes."""
from __future__ import annotations

from angel_auto.strategy.base import Strategy
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy
from angel_auto.strategy.macd_zero_cross_single_leg import MacdZeroCrossSingleLegStrategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "macd_itm_otm_spread": MacdItmOtmSpreadStrategy,
    "atm_sell_macd_zero": MacdZeroCrossSingleLegStrategy,
    "itm4_buy_macd_zero": MacdZeroCrossSingleLegStrategy,
}


def get_strategy_class(name: str) -> type[Strategy]:
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown strategy '{name}' - registered: {list(STRATEGY_REGISTRY)}") from exc
