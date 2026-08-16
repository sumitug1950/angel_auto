from angel_auto.broker.base import MarginLeg, OrderRequest
from angel_auto.broker.paper_broker import PaperBroker
from angel_auto.core.enums import OrderSide, OrderStatus
from angel_auto.data.market_data import OptionChainSnapshot


def _chain_with_quote(token="1", symbol="NIFTY18AUG2624600CE", strike=24600.0, ltp=300.0) -> OptionChainSnapshot:
    chain = OptionChainSnapshot()
    chain.register(token, symbol, strike, "CE")
    chain.update_ltp(token, ltp)
    return chain


def test_market_buy_order_fills_above_ltp_with_slippage():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000, slippage_pct=0.1)

    result = broker.place_order(
        OrderRequest(
            exchange="NFO", trading_symbol="NIFTY18AUG2624600CE", token="1", side=OrderSide.BUY,
            quantity=65, order_type="MARKET", product_type="INTRADAY",
        )
    )
    assert result.status == OrderStatus.FILLED
    assert result.fill_price > 300.0  # buy slips up
    assert result.fill_price == round(300.0 * 1.001, 2)


def test_market_sell_order_fills_below_ltp_with_slippage():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000, slippage_pct=0.1)

    result = broker.place_order(
        OrderRequest(
            exchange="NFO", trading_symbol="NIFTY18AUG2624600CE", token="1", side=OrderSide.SELL,
            quantity=65, order_type="MARKET", product_type="INTRADAY",
        )
    )
    assert result.status == OrderStatus.FILLED
    assert result.fill_price < 300.0


def test_order_rejected_when_no_live_quote():
    chain = OptionChainSnapshot()  # nothing registered
    broker = PaperBroker(chain, starting_capital_rs=100000)

    result = broker.place_order(
        OrderRequest(
            exchange="NFO", trading_symbol="X", token="unknown", side=OrderSide.BUY,
            quantity=65, order_type="MARKET", product_type="INTRADAY",
        )
    )
    assert result.status == OrderStatus.REJECTED


def test_limit_order_left_open_when_not_marketable():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000, slippage_pct=0.1)

    # buy limit far below the fill price - should not fill
    result = broker.place_order(
        OrderRequest(
            exchange="NFO", trading_symbol="NIFTY18AUG2624600CE", token="1", side=OrderSide.BUY,
            quantity=65, order_type="LIMIT", product_type="INTRADAY", price=200.0,
        )
    )
    assert result.status == OrderStatus.OPEN
    assert broker.get_order_status(result.broker_order_id) == OrderStatus.OPEN


def test_limit_order_fills_when_marketable():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000, slippage_pct=0.1)

    result = broker.place_order(
        OrderRequest(
            exchange="NFO", trading_symbol="NIFTY18AUG2624600CE", token="1", side=OrderSide.BUY,
            quantity=65, order_type="LIMIT", product_type="INTRADAY", price=301.0,
        )
    )
    assert result.status == OrderStatus.FILLED


def test_cancel_order():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000)
    result = broker.place_order(
        OrderRequest(
            exchange="NFO", trading_symbol="NIFTY18AUG2624600CE", token="1", side=OrderSide.BUY,
            quantity=65, order_type="LIMIT", product_type="INTRADAY", price=100.0,
        )
    )
    assert result.status == OrderStatus.OPEN
    broker.cancel_order(result.broker_order_id)
    assert broker.get_order_status(result.broker_order_id) == OrderStatus.CANCELLED


def test_check_margin_debit_leg_costs_full_premium():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000)
    result = broker.check_margin([MarginLeg("NFO", "X", "1", OrderSide.BUY, 65, "INTRADAY")])
    assert result.required_margin_rs == 300.0 * 65
    assert result.is_affordable is True


def test_check_margin_rejects_when_unaffordable():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=1000)  # tiny capital
    result = broker.check_margin([MarginLeg("NFO", "X", "1", OrderSide.BUY, 65, "INTRADAY")])
    assert result.is_affordable is False


def test_positions_track_and_net_to_zero_on_opposite_fill():
    chain = _chain_with_quote(ltp=300.0)
    broker = PaperBroker(chain, starting_capital_rs=100000)

    broker.place_order(
        OrderRequest("NFO", "X", "1", OrderSide.BUY, 65, "MARKET", "INTRADAY")
    )
    assert len(broker.get_positions()) == 1

    broker.place_order(
        OrderRequest("NFO", "X", "1", OrderSide.SELL, 65, "MARKET", "INTRADAY")
    )
    assert len(broker.get_positions()) == 0  # fully closed out
