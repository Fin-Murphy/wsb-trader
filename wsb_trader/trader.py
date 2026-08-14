"""Alpaca paper-trading client.

Wraps the pieces of Alpaca's REST API we actually use for this bot:
account state, current positions, market orders, and position closes.

Uses raw ``httpx`` rather than the ``alpaca-py`` SDK so the transport is
mockable in tests and doesn't couple us to a specific SDK version.

Docs: https://docs.alpaca.markets/reference/getaccount
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import httpx
import structlog

log = structlog.get_logger()


PAPER_BASE_URL = "https://paper-api.alpaca.markets"

OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class Account:
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    equity: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    market_value: Decimal
    unrealized_pl: Decimal
    avg_entry_price: Decimal
    current_price: Decimal | None = None
    position_type: str = "long"  # "long" or "short"


@dataclass(frozen=True)
class OrderResult:
    id: str
    symbol: str
    qty: Decimal
    side: OrderSide
    status: str


class AlpacaError(Exception):
    """Alpaca returned an error we should surface to the caller."""


class AssetNotTradable(AlpacaError):
    """Alpaca rejected the order because the asset is not tradable (code 42210000)."""


class PaperTrader:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = PAPER_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ):
        if not api_key or not api_secret:
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET must be set")
        # Guard against pointing at the live endpoint by accident.
        if "paper" not in base_url:
            raise ValueError(
                f"refusing to run against non-paper endpoint: {base_url!r}. "
                "This bot is paper-only until further review."
            )
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_account(self) -> Account:
        data = await self._get("/v2/account")
        return Account(
            cash=Decimal(data["cash"]),
            buying_power=Decimal(data["buying_power"]),
            portfolio_value=Decimal(data["portfolio_value"]),
            equity=Decimal(data["equity"]),
        )

    async def list_positions(self) -> list[Position]:
        data = await self._get("/v2/positions")
        return [_parse_position(p) for p in data]

    async def get_position(self, symbol: str) -> Position | None:
        try:
            data = await self._get(f"/v2/positions/{symbol}")
        except AlpacaError as e:
            # Alpaca returns 404 when the position doesn't exist; treat as
            # "no position" rather than an error.
            if "404" in str(e):
                return None
            raise
        return _parse_position(data)

    async def place_market_order(
        self,
        symbol: str,
        *,
        notional: Decimal | float | None = None,
        qty: Decimal | float | None = None,
        side: OrderSide = "buy",
        time_in_force: str | None = None,
    ) -> OrderResult:
        """Submit a market order. Provide either ``notional`` (dollar amount)
        or ``qty`` (share count), not both.

        ``time_in_force`` defaults to ``"gtc"`` for crypto symbols (Alpaca
        rejects ``"day"`` on crypto) and ``"day"`` for stocks.
        """
        if (notional is None) == (qty is None):
            raise ValueError("exactly one of notional or qty must be provided")
        if time_in_force is None:
            time_in_force = "gtc" if _is_crypto_symbol(symbol) else "day"
        payload: dict = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
        }
        if notional is not None:
            payload["notional"] = str(notional)
        else:
            payload["qty"] = str(qty)
        data = await self._post("/v2/orders", payload)
        return OrderResult(
            id=data["id"],
            symbol=data["symbol"],
            qty=Decimal(data.get("qty") or "0"),
            side=data["side"],
            status=data["status"],
        )

    async def close_position(self, symbol: str) -> OrderResult | None:
        """Liquidate an entire position. Returns None if no position existed."""
        try:
            data = await self._delete(f"/v2/positions/{symbol}")
        except AlpacaError as e:
            if "404" in str(e):
                return None
            raise
        return OrderResult(
            id=data["id"],
            symbol=data["symbol"],
            qty=Decimal(data.get("qty") or "0"),
            side=data["side"],
            status=data["status"],
        )

    async def place_short_order(
        self,
        symbol: str,
        *,
        notional: Decimal | float | None = None,
        qty: Decimal | float | None = None,
        time_in_force: str | None = None,
    ) -> OrderResult:
        """Submit a short sell order. Provide either ``notional`` or ``qty``.

        Alpaca does not support shorting crypto — callers should guard against
        crypto symbols before calling this.
        """
        return await self.place_market_order(
            symbol,
            notional=notional,
            qty=qty,
            side="sell",
            time_in_force=time_in_force,
        )

    async def get_current_price(self, symbol: str) -> Decimal | None:
        """Fetch current price from Yahoo Finance.

        Crypto symbols in Alpaca format (``BTCUSD``) are converted to Yahoo's
        format (``BTC-USD``) before the request.
        """
        yahoo_symbol = _to_yahoo_symbol(symbol)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://query1.finance.yahoo.com/v10/finance/quoteSummary/" + yahoo_symbol,
                    params={"modules": "price"},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                price = data.get("quoteSummary", {}).get("result", [{}])[0].get("price", {}).get("regularMarketPrice")
                if price:
                    return Decimal(str(price))
                return None
        except Exception:
            log.warning("failed_to_fetch_price", symbol=symbol)
            return None

    async def _get(self, path: str) -> dict | list:
        resp = await self._client.get(f"{self.base_url}{path}", headers=self._headers)
        return _unwrap(resp)

    async def _post(self, path: str, json_body: dict) -> dict:
        resp = await self._client.post(
            f"{self.base_url}{path}", json=json_body, headers=self._headers,
        )
        return _unwrap(resp)

    async def _delete(self, path: str) -> dict:
        resp = await self._client.delete(f"{self.base_url}{path}", headers=self._headers)
        return _unwrap(resp)


def _unwrap(resp: httpx.Response) -> dict | list:
    if resp.status_code >= 400:
        try:
            code = resp.json().get("code")
        except Exception:
            code = None
        if code == 42210000:
            raise AssetNotTradable(resp.json().get("message", "asset not tradable"))
        raise AlpacaError(f"[{resp.status_code}] {resp.text}")
    return resp.json()


def _parse_position(d: dict) -> Position:
    return Position(
        symbol=d["symbol"],
        qty=Decimal(d["qty"]),
        market_value=Decimal(d["market_value"]),
        unrealized_pl=Decimal(d["unrealized_pl"]),
        avg_entry_price=Decimal(d["avg_entry_price"]),
    )


def _is_crypto_symbol(symbol: str) -> bool:
    """Heuristic: any symbol ending in 'USD' with a 2-5 letter base is crypto.

    Matches Alpaca's crypto format (``BTCUSD``, ``ETHUSD``, ``DOGEUSD``).
    Regular stock tickers never end in USD.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USD") or len(symbol) <= 3:
        return False
    base = symbol[:-3]
    return base.isalpha() and 2 <= len(base) <= 5


def _to_yahoo_symbol(symbol: str) -> str:
    """Convert an Alpaca symbol to Yahoo's format.

    Yahoo uses ``BTC-USD`` where Alpaca uses ``BTCUSD``. Stocks are unchanged.
    """
    symbol = symbol.upper()
    if symbol.endswith("USD") and len(symbol) > 3 and "-" not in symbol:
        base = symbol[:-3]
        if base.isalpha() and 2 <= len(base) <= 5:
            return f"{base}-USD"
    return symbol
