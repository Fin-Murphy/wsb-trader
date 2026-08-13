from decimal import Decimal

import httpx
import pytest
import respx

from wsb_trader.trader import (
    Account,
    AlpacaError,
    OrderResult,
    PaperTrader,
    Position,
    _parse_position,
)


@pytest.fixture
def trader():
    return PaperTrader(api_key="test-key", api_secret="test-secret")


class TestPaperTraderInit:
    def test_rejects_missing_credentials(self):
        with pytest.raises(ValueError, match="ALPACA_API_KEY"):
            PaperTrader(api_key="", api_secret="s")
        with pytest.raises(ValueError, match="ALPACA_API_KEY"):
            PaperTrader(api_key="k", api_secret="")

    def test_rejects_live_endpoint(self):
        # Guard: if someone accidentally sets ALPACA_BASE_URL to the live URL,
        # blow up before placing a real trade.
        with pytest.raises(ValueError, match="paper-only"):
            PaperTrader(
                api_key="k", api_secret="s",
                base_url="https://api.alpaca.markets",
            )


class TestGetAccount:
    @respx.mock
    async def test_parses_account(self, trader):
        respx.get("https://paper-api.alpaca.markets/v2/account").mock(
            return_value=httpx.Response(200, json={
                "cash": "100000.00",
                "buying_power": "200000.00",
                "portfolio_value": "105000.50",
                "equity": "105000.50",
            })
        )
        acct = await trader.get_account()
        await trader.close()
        assert acct == Account(
            cash=Decimal("100000.00"),
            buying_power=Decimal("200000.00"),
            portfolio_value=Decimal("105000.50"),
            equity=Decimal("105000.50"),
        )

    @respx.mock
    async def test_sends_auth_headers(self, trader):
        route = respx.get("https://paper-api.alpaca.markets/v2/account").mock(
            return_value=httpx.Response(200, json={
                "cash": "0", "buying_power": "0",
                "portfolio_value": "0", "equity": "0",
            })
        )
        await trader.get_account()
        await trader.close()
        req = route.calls.last.request
        assert req.headers["apca-api-key-id"] == "test-key"
        assert req.headers["apca-api-secret-key"] == "test-secret"


class TestPositions:
    @respx.mock
    async def test_list_positions_parses_multiple(self, trader):
        respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
            return_value=httpx.Response(200, json=[
                {
                    "symbol": "AAPL", "qty": "10", "market_value": "1750.00",
                    "unrealized_pl": "50.00", "avg_entry_price": "170.00",
                },
                {
                    "symbol": "GME", "qty": "5", "market_value": "125.00",
                    "unrealized_pl": "-25.00", "avg_entry_price": "30.00",
                },
            ])
        )
        positions = await trader.list_positions()
        await trader.close()
        assert len(positions) == 2
        assert positions[0].symbol == "AAPL"
        assert positions[0].qty == Decimal("10")
        assert positions[1].unrealized_pl == Decimal("-25.00")

    @respx.mock
    async def test_get_position_found(self, trader):
        respx.get("https://paper-api.alpaca.markets/v2/positions/AAPL").mock(
            return_value=httpx.Response(200, json={
                "symbol": "AAPL", "qty": "10", "market_value": "1750.00",
                "unrealized_pl": "50.00", "avg_entry_price": "170.00",
            })
        )
        pos = await trader.get_position("AAPL")
        await trader.close()
        assert pos is not None
        assert pos.symbol == "AAPL"

    @respx.mock
    async def test_get_position_returns_none_on_404(self, trader):
        respx.get("https://paper-api.alpaca.markets/v2/positions/XXX").mock(
            return_value=httpx.Response(404, json={"code": 40410000, "message": "position does not exist"})
        )
        pos = await trader.get_position("XXX")
        await trader.close()
        assert pos is None


class TestPlaceOrder:
    @respx.mock
    async def test_market_order_by_notional(self, trader):
        route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
            return_value=httpx.Response(200, json={
                "id": "order-1", "symbol": "AAPL", "qty": "5.88",
                "side": "buy", "status": "accepted",
            })
        )
        result = await trader.place_market_order("AAPL", notional=1000, side="buy")
        await trader.close()
        assert result == OrderResult(
            id="order-1", symbol="AAPL", qty=Decimal("5.88"),
            side="buy", status="accepted",
        )
        import json as _json
        body = _json.loads(route.calls.last.request.content)
        assert body["notional"] == "1000"
        assert body["type"] == "market"
        assert "qty" not in body

    @respx.mock
    async def test_market_order_by_qty(self, trader):
        route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
            return_value=httpx.Response(200, json={
                "id": "order-2", "symbol": "AAPL", "qty": "10",
                "side": "buy", "status": "accepted",
            })
        )
        await trader.place_market_order("AAPL", qty=10, side="buy")
        await trader.close()
        import json as _json
        body = _json.loads(route.calls.last.request.content)
        assert body["qty"] == "10"
        assert "notional" not in body

    async def test_rejects_missing_qty_and_notional(self, trader):
        with pytest.raises(ValueError, match="exactly one of notional or qty"):
            await trader.place_market_order("AAPL")

    async def test_rejects_both_qty_and_notional(self, trader):
        with pytest.raises(ValueError, match="exactly one of notional or qty"):
            await trader.place_market_order("AAPL", notional=1000, qty=10)

    @respx.mock
    async def test_raises_on_alpaca_error(self, trader):
        respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
            return_value=httpx.Response(422, text='{"code": 40010001, "message": "buying power exceeded"}')
        )
        with pytest.raises(AlpacaError, match="422"):
            await trader.place_market_order("AAPL", notional=1_000_000)
        await trader.close()


class TestClosePosition:
    @respx.mock
    async def test_close_returns_order(self, trader):
        respx.delete("https://paper-api.alpaca.markets/v2/positions/AAPL").mock(
            return_value=httpx.Response(200, json={
                "id": "close-1", "symbol": "AAPL", "qty": "10",
                "side": "sell", "status": "accepted",
            })
        )
        result = await trader.close_position("AAPL")
        await trader.close()
        assert result is not None
        assert result.side == "sell"

    @respx.mock
    async def test_close_returns_none_on_404(self, trader):
        respx.delete("https://paper-api.alpaca.markets/v2/positions/XXX").mock(
            return_value=httpx.Response(404, text="position does not exist")
        )
        result = await trader.close_position("XXX")
        await trader.close()
        assert result is None


class TestParsePosition:
    def test_parses_decimals(self):
        pos = _parse_position({
            "symbol": "AAPL", "qty": "5.5", "market_value": "962.50",
            "unrealized_pl": "12.34", "avg_entry_price": "175.00",
        })
        assert isinstance(pos.qty, Decimal)
        assert pos.qty == Decimal("5.5")
        assert pos.unrealized_pl == Decimal("12.34")
