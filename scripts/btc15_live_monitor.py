#!/usr/bin/env python3
"""Live BTC15 monitor for the default three-lane strategy.

- Shows Kalshi balance.
- Tracks the current KXBTC15M 15-minute market.
- Shows live UP/DOWN bid/ask stats.
- Evaluates Reclaim-70, BTC-Fade-90, and NO-Reclaim-80.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXBTC15M"
UI_WIDTH = 55
DEFAULT_TRADE_LOG = ROOT / "reports" / "btc15_trade_log.json"
MARKET_TIMEZONE = ZoneInfo("America/New_York")
BTC15_EXCHANGE_INDEX = 2
DEFAULT_STARTING_BALANCE_CENTS = 11_500
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

RECLAIM_STRATEGY = "RECLAIM_70"
RECLAIM_DISPLAY = "Reclaim-70"
BTC_FADE_STRATEGY = "BTC_FADE_90"
BTC_FADE_DISPLAY = "BTC-Fade-90"
NO_RECLAIM_STRATEGY = "NO_RECLAIM_80"
NO_RECLAIM_DISPLAY = "NO-Reclaim-80"
RECLAIM_DECISION_MINUTE = 5
RECLAIM_ENTRY_MINUTE_MIN = 6
RECLAIM_ENTRY_MINUTE_MAX = 10
RECLAIM_EARLY_LOW_MIN = 0.15
RECLAIM_EARLY_LOW_MAX = 0.30
RECLAIM_ENTRY_MIN = 0.35
RECLAIM_ENTRY_MAX = 0.55
RECLAIM_BTC_RET_MIN = -20.0
RECLAIM_BTC_RET_MAX = 20.0
RECLAIM_EMA_DIST_MIN = -150.0
RECLAIM_EMA_DIST_MAX = -50.0
RECLAIM_TARGET = 0.70
# Fallback for paper positions written before exits became strategy-specific.
PAPER_EXIT_TRIGGER_CENTS = 71
TAKER_FEE_RATE = 0.07
DEFAULT_ENTRY_CONTRACTS = 5
DEFAULT_RISK_PCT = 20.0
ENTRY_FAILURE_COOLDOWN_SECONDS = 60
NO_MARKET_RELOAD_ATTEMPTS = 5
NO_MARKET_RELOAD_SLEEP_SECONDS = 5.0
BTC15_DIRECT_LOOKAHEAD_WINDOWS = 4
ROLLOVER_WAIT_SECONDS = 90
CANDLE_REFRESH_SECONDS = 15
CANDLE_429_BACKOFF_SECONDS = 60
BTC_CONTEXT_REFRESH_SECONDS = 60
ENTRY_CUTOFF_SECONDS = 5 * 60


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def estimated_taker_fee_cents(price_cents: int, count: int) -> float:
    """Estimate Kalshi's general quadratic taker fee, rounded to a centicent."""
    if price_cents <= 0 or price_cents >= 100 or count <= 0:
        return 0.0
    price = price_cents / 100
    fee_dollars = TAKER_FEE_RATE * count * price * (1 - price)
    # Remove binary-float dust so exact centicent values do not round up again.
    return math.ceil(fee_dollars * 10_000 - 1e-9) / 100


def estimated_round_trip_fee_cents(entry_cents: int, exit_cents: int, count: int) -> float:
    return estimated_taker_fee_cents(entry_cents, count) + estimated_taker_fee_cents(exit_cents, count)


def parse_ts(value: object) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def next_quarter_hour(value: dt.datetime) -> dt.datetime:
    value = value.astimezone(MARKET_TIMEZONE).replace(second=0, microsecond=0)
    minute = ((value.minute // 15) + 1) * 15
    if minute >= 60:
        return (value.replace(minute=0) + dt.timedelta(hours=1))
    return value.replace(minute=minute)


def btc15_event_tickers(now: dt.datetime) -> list[str]:
    close_time = next_quarter_hour(now)
    start_time = close_time - dt.timedelta(minutes=15)
    tickers = []
    for step in range(BTC15_DIRECT_LOOKAHEAD_WINDOWS + 1):
        candidate = start_time + dt.timedelta(minutes=15 * step)
        suffix = candidate.strftime("%d%b%y%H%M").upper()
        tickers.append(f"{SERIES_TICKER}-{suffix}")
    return tickers


def market_close_from_ticker(ticker: str) -> dt.datetime | None:
    match = re.match(rf"^{SERIES_TICKER}-(\d{{2}}[A-Z]{{3}}\d{{2}}\d{{4}})-", ticker)
    if not match:
        return None
    try:
        local = dt.datetime.strptime(match.group(1), "%y%b%d%H%M").replace(tzinfo=MARKET_TIMEZONE)
    except ValueError:
        return None
    return local.astimezone(dt.timezone.utc)


def seconds_after_quarter_hour(now: dt.datetime) -> int:
    local = now.astimezone(MARKET_TIMEZONE)
    return (local.minute % 15) * 60 + local.second


def in_rollover_wait(now: dt.datetime) -> bool:
    return seconds_after_quarter_hour(now) <= ROLLOVER_WAIT_SECONDS


def money(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cents(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value * 100:5.1f}c"


def plain_cents(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value * 100:.1f}c"


def dollars_from_cents(value: object) -> str:
    if value is None:
        return "--"
    try:
        return f"${float(value) / 100:,.2f}"
    except (TypeError, ValueError):
        return "--"


def account_cents(balance: dict) -> tuple[int, int, int]:
    cash_cents = int(money(balance.get("balance")) or 0)
    portfolio_cents = int(money(balance.get("portfolio_value")) or 0)
    return cash_cents, portfolio_cents, cash_cents + portfolio_cents


def pct_from_start(current_cents: int, starting_cents: int) -> float | None:
    if starting_cents <= 0:
        return None
    return (current_cents - starting_cents) / starting_cents * 100


def format_account_pnl(current_cents: int, starting_cents: int) -> str:
    pct = pct_from_start(current_cents, starting_cents)
    if pct is None:
        return "--"
    pnl_cents = current_cents - starting_cents
    return f"{dollars_from_cents(pnl_cents)} ({pct:+.2f}%)"


def format_pct(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.1f}%"


def format_profit_factor(value: float | None, wins: int, losses: int) -> str:
    if value is None:
        return "inf" if wins and not losses else "--"
    return f"{value:.2f}"


class Ui:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"

    def __init__(self, color: bool = True):
        self.color = color

    def s(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + self.RESET


def fmt_bool(ok: bool, ui: Ui) -> str:
    return ui.s("OK", Ui.GREEN, Ui.BOLD) if ok else ui.s("--", Ui.DIM)


def fmt_signal_name(name: str, ui: Ui) -> str:
    return ui.s(name, Ui.GREEN, Ui.BOLD) if name else ui.s("WAIT", Ui.DIM)


def display_side(side: str) -> str:
    side = side.lower()
    if side == "yes":
        return "UP"
    if side == "no":
        return "DOWN"
    return side.upper()


def colored_side(side: str, ui: Ui, width: int = 0) -> str:
    label = display_side(side)
    padded = f"{label:<{width}}" if width else label
    color_code = Ui.GREEN if label == "UP" else Ui.RED if label == "DOWN" else ""
    return ui.s(padded, color_code, Ui.BOLD) if color_code else padded


def colored_action(action: str, ui: Ui, width: int = 0) -> str:
    label = action.upper()
    padded = f"{label:<{width}}" if width else label
    color_code = Ui.GREEN if label == "BUY" else Ui.RED if label == "SELL" else ""
    return ui.s(padded, color_code, Ui.BOLD) if color_code else padded


def display_strategy(name: str) -> str:
    if name == RECLAIM_STRATEGY:
        return RECLAIM_DISPLAY
    if name == BTC_FADE_STRATEGY:
        return BTC_FADE_DISPLAY
    if name == NO_RECLAIM_STRATEGY:
        return NO_RECLAIM_DISPLAY
    if name.startswith("YES_"):
        return "UP_" + name[4:]
    if name.startswith("NO_"):
        return "DOWN_" + name[3:]
    return name


def format_clock(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{minutes:02d}:{remainder:02d}"


def title_rule(title: str, ui: Ui, width: int = UI_WIDTH) -> str:
    padding = max(2, width - len(title) - 2)
    left = padding // 2
    right = padding - left
    return ui.s(f"{'-' * left} {title} {'-' * right}", Ui.BOLD, Ui.BLUE)


def section_rule(title: str, ui: Ui, width: int = UI_WIDTH) -> str:
    return title_rule(title, ui, width)


def quote_spread(side: SideQuote) -> float | None:
    if side.ask is None or side.bid is None:
        return None
    return side.ask - side.bid


def quote_spread_value(bid: float | None, ask: float | None) -> float | None:
    if ask is None or bid is None:
        return None
    return ask - bid


def price_to_cents(price: float | None) -> int | None:
    if price is None:
        return None
    return max(1, min(99, int(round(price * 100))))


def cents_to_fixed_dollars(price_cents: int) -> str:
    return f"{max(1, min(99, int(price_cents))) / 100:.4f}"


def api_cents(record: dict, *keys: str) -> int:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if key.endswith("_dollars"):
            return int(round(amount * 100))
        return int(round(amount))
    return 0


def order_price_cents(order: dict, side: str) -> int | None:
    side = side.lower()
    if side == "no":
        explicit_no = api_cents(order, "no_price_dollars", "no_price")
        if explicit_no > 0:
            return explicit_no
        yes_price = api_cents(order, "yes_price_dollars", "yes_price", "price_dollars", "price")
        if yes_price > 0:
            return 100 - yes_price
    elif side == "yes":
        explicit_yes = api_cents(order, "yes_price_dollars", "yes_price", "price_dollars", "price")
        if explicit_yes > 0:
            return explicit_yes
    keys = (
        (f"{side}_price_dollars", f"{side}_price", "price_dollars", "price")
        if side in ("yes", "no")
        else ("price_dollars", "price")
    )
    for key in keys:
        if key not in order or order.get(key) is None:
            continue
        cents_value = api_cents(order, key)
        if cents_value > 0:
            return cents_value
    return None


def order_intent(order: dict) -> tuple[str, str, int | None]:
    """Translate Kalshi's YES-book V2 fields into BTC15 yes/up and no/down intent."""
    raw_action = str(order.get("action") or "").lower()
    book_side = str(order.get("book_side") or order.get("side") or "").lower()
    outcome_side = str(order.get("outcome_side") or "").lower()
    yes_cents = api_cents(order, "yes_price_dollars", "yes_price", "price_dollars", "price")
    no_cents = api_cents(order, "no_price_dollars", "no_price")

    if raw_action == "buy" and book_side == "bid":
        if no_cents >= int(RECLAIM_TARGET * 100) and 0 < yes_cents <= 100 - int(RECLAIM_TARGET * 100):
            return "sell", "no", no_cents
        return "buy", "yes", yes_cents or None

    if raw_action == "sell" and book_side == "ask":
        if yes_cents >= int(RECLAIM_TARGET * 100) and 0 < no_cents <= 100 - int(RECLAIM_TARGET * 100):
            return "sell", "yes", yes_cents
        return "buy", "no", no_cents or None

    if outcome_side in ("yes", "no"):
        return raw_action, outcome_side, order_price_cents(order, outcome_side)
    return raw_action, "", None


def load_private_key(path: Path):
    path = path.expanduser()
    if not path.is_absolute() and not path.exists():
        project_path = ROOT / path
        if project_path.exists():
            path = project_path
        else:
            fallback = ROOT / path.name
            if fallback.exists():
                path = fallback
    with path.open("rb") as fh:
        return serialization.load_pem_private_key(
            fh.read(),
            password=None,
            backend=default_backend(),
        )


class KalshiClient:
    def __init__(self, base_url: str, api_key: str | None, private_key_path: str | None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.api_key = api_key
        self.private_key = load_private_key(Path(private_key_path)) if private_key_path else None

    def _signature(self, method: str, path: str, timestamp_ms: str) -> str:
        if self.private_key is None:
            raise RuntimeError("Authenticated endpoint requires KALSHI_PRIVATE_KEY_PATH")
        sign_path = urlparse(self.base_url + path).path
        message = f"{timestamp_ms}{method.upper()}{sign_path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def request(
        self,
        method: str,
        path: str,
        params: dict | list[tuple[str, str]] | None = None,
        json_body: dict | None = None,
        auth: bool = False,
    ) -> dict:
        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)
        url = self.base_url + path + query
        headers = {}
        if auth:
            if not self.api_key:
                raise RuntimeError("Authenticated endpoint requires KALSHI_API_KEY")
            timestamp_ms = str(int(time.time() * 1000))
            method = method.upper()
            headers = {
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
                "KALSHI-ACCESS-SIGNATURE": self._signature(method, path, timestamp_ms),
                "Content-Type": "application/json",
            }
        response = self.session.request(method, url, headers=headers, json=json_body, timeout=15)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text.strip()
            message = f"{exc}"
            if body:
                message = f"{message} | {body[:500]}"
            raise requests.HTTPError(message, response=response) from exc
        return response.json()

    def get(self, path: str, params: dict | list[tuple[str, str]] | None = None, auth: bool = False) -> dict:
        return self.request("GET", path, params=params, auth=auth)

    def balance(self) -> dict:
        return self.get("/portfolio/balance", auth=True)

    def positions(self, settlement_status: str = "unsettled", limit: int = 100) -> dict:
        params: dict[str, object] = {"limit": limit}
        if settlement_status:
            params["settlement_status"] = settlement_status
        return self.get("/portfolio/positions", params=params, auth=True)

    def orders(self, status: str = "resting", limit: int = 100) -> dict:
        params: dict[str, object] = {"limit": limit}
        if status:
            params["status"] = status
        return self.get("/portfolio/orders", params=params, auth=True)

    def fills(self, limit: int = 1000) -> dict:
        return self.get("/portfolio/fills", params={"limit": limit}, auth=True)

    def settlements(self, limit: int = 1000) -> dict:
        return self.get("/portfolio/settlements", params={"limit": limit}, auth=True)

    def create_order(self, ticker: str, action: str, side: str, count: int, price_cents: int) -> dict:
        side = side.lower()
        action = action.lower()
        if side not in ("yes", "no"):
            raise ValueError(f"unsupported side: {side}")
        if action not in ("buy", "sell"):
            raise ValueError(f"unsupported action: {action}")

        if side == "yes":
            book_side = "bid" if action == "buy" else "ask"
            yes_price_cents = price_cents
        else:
            book_side = "ask" if action == "buy" else "bid"
            yes_price_cents = 100 - price_cents

        body: dict[str, object] = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": book_side,
            "count": f"{count:.2f}",
            "price": cents_to_fixed_dollars(yes_price_cents),
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "exchange_index": BTC15_EXCHANGE_INDEX,
        }
        result = self.request("POST", "/portfolio/events/orders", json_body=body, auth=True)
        if "order" in result:
            return result
        return {
            "order": {
                **result,
                "ticker": ticker,
                "action": action,
                "side": side,
                "count": count,
                "remaining_count": result.get("remaining_count", count),
                "price": price_cents,
                "client_order_id": body["client_order_id"],
            }
        }

    def order(self, order_id: str) -> dict:
        return self.get(f"/portfolio/orders/{order_id}", auth=True)

    def open_btc15_markets(self, now: dt.datetime) -> list[dict]:
        markets = []
        seen = set()

        def add_rows(rows: list[dict]) -> None:
            for row in rows:
                ticker = row.get("ticker")
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)
                markets.append(row)

        for status in ("open", "active"):
            try:
                payload = self.get(
                    "/markets",
                    params={"series_ticker": SERIES_TICKER, "status": status, "limit": 100},
                )
                add_rows(payload.get("markets", []))
            except requests.HTTPError:
                continue

        for event_ticker in btc15_event_tickers(now):
            try:
                payload = self.get(
                    "/markets",
                    params={"event_ticker": event_ticker, "limit": 100},
                )
                add_rows(payload.get("markets", []))
            except requests.HTTPError:
                continue

        return markets

    def orderbook(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}/orderbook", params={"depth": 20}, auth=True)

    def candlesticks(self, series_or_event_ticker: str, ticker: str, start_ts: int, end_ts: int) -> list[dict]:
        payload = self.get(
            f"/series/{series_or_event_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1},
        )
        return payload.get("candlesticks", [])


@dataclass
class SideQuote:
    side: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    prior_peak_ask: float | None
    prior_low_ask: float | None
    prior_close_ask: float | None
    last_ask_open: float | None
    last_ask_close: float | None
    last_ask_low: float | None
    last_ask_high: float | None
    last_range: float | None
    last_body: float | None
    drop_from_prior_high: float | None
    pullback: float | None
    live_match: bool
    strict_match: bool
    reclaim_signal: bool
    reclaim_name: str


@dataclass
class ActivePosition:
    ticker: str
    side: str
    qty: int
    cost_cents: int
    mark_cents: int | None
    value_cents: int | None
    unrealized_cents: int | None
    realized_cents: int
    fees_cents: int


@dataclass
class ActiveOrder:
    order_id: str
    ticker: str
    action: str
    side: str
    count: int
    remaining_count: int
    price_cents: int | None
    status: str
    created_time: str


@dataclass
class TradeSignal:
    ticker: str
    strategy: str
    side: str
    count: int
    entry_cents: int
    target_cents: int
    reason: str


@dataclass
class BtcContext:
    available: bool
    btc_open_first: float | None = None
    btc_close_decision: float | None = None
    btc_ret_window: float | None = None
    btc15_ema21_decision: float | None = None
    signed_dist_ema21_15m: float | None = None
    error: str | None = None


@dataclass
class ReclaimState:
    strategy: str
    display_strategy: str
    side: str
    minute: int
    early_ask_low: float | None
    decision_ask_close: float | None
    entry_ask_open: float | None
    entry_ask_close: float | None
    live_ask: float | None
    btc_context: BtcContext
    checks: list[tuple[str, bool]]
    ready: bool
    entry_cents: int | None
    target_cents: int


@dataclass
class BotAction:
    status: str
    message: str


@dataclass
class BotStats:
    closed: int
    wins: int
    losses: int
    realized_cents: int
    total_cost_cents: int
    avg_win_cents: int | None
    avg_loss_cents: int | None
    avg_roi_pct: float | None
    max_loss_cents: int | None
    profit_factor: float | None
    total_fees_cents: int = 0
    gross_loss_cents: int = 0


@dataclass
class PaperAccountStats:
    closed: int
    wins: int
    losses: int
    realized_cents: int
    open_positions: int
    open_contracts: int
    open_cost_cents: int
    open_value_cents: int
    open_unrealized_cents: int
    equity_cents: int
    total_fees_cents: int
    avg_win_cents: int | None
    avg_loss_cents: int | None
    profit_factor: float | None


def best_book_level(levels: list | None) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    parsed = []
    for level in levels:
        if len(level) < 2:
            continue
        price = money(level[0])
        size = money(level[1])
        if price is not None:
            parsed.append((price, size))
    if not parsed:
        return None, None
    return max(parsed, key=lambda item: item[0])


def side_ask_from_candle(candle: dict, side: str) -> float | None:
    yes_ask = candle.get("yes_ask", {}) or {}
    yes_bid = candle.get("yes_bid", {}) or {}
    if side == "YES":
        return money(yes_ask.get("close_dollars", yes_ask.get("close")))
    yes_bid_close = money(yes_bid.get("close_dollars", yes_bid.get("close")))
    if yes_bid_close is None:
        return None
    return 1.0 - yes_bid_close


def side_candle_values(candle: dict, side: str) -> dict[str, float | None]:
    yes_ask = candle.get("yes_ask", {}) or {}
    yes_bid = candle.get("yes_bid", {}) or {}
    if side == "YES":
        ask_open = money(yes_ask.get("open_dollars", yes_ask.get("open")))
        ask_close = money(yes_ask.get("close_dollars", yes_ask.get("close")))
        ask_low = money(yes_ask.get("low_dollars", yes_ask.get("low")))
        ask_high = money(yes_ask.get("high_dollars", yes_ask.get("high")))
    else:
        bid_open = money(yes_bid.get("open_dollars", yes_bid.get("open")))
        bid_close = money(yes_bid.get("close_dollars", yes_bid.get("close")))
        bid_low = money(yes_bid.get("low_dollars", yes_bid.get("low")))
        bid_high = money(yes_bid.get("high_dollars", yes_bid.get("high")))
        ask_open = 1.0 - bid_open if bid_open is not None else None
        ask_close = 1.0 - bid_close if bid_close is not None else None
        ask_low = 1.0 - bid_high if bid_high is not None else None
        ask_high = 1.0 - bid_low if bid_low is not None else None
    return {
        "ask_open": ask_open,
        "ask_close": ask_close,
        "ask_low": ask_low,
        "ask_high": ask_high,
    }


def choose_current_market(markets: list[dict], now: dt.datetime) -> dict | None:
    candidates = []
    for market in markets:
        open_time = parse_ts(market.get("open_time"))
        close_time = parse_ts(market.get("close_time"))
        if not open_time or not close_time:
            continue
        if open_time <= now < close_time:
            candidates.append((open_time, market))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    future = []
    for market in markets:
        open_time = parse_ts(market.get("open_time"))
        if open_time and open_time > now:
            future.append((open_time, market))
    if future:
        return min(future, key=lambda item: item[0])[1]
    return None


def market_is_current(market: dict, now: dt.datetime) -> bool:
    open_time = parse_ts(market.get("open_time"))
    close_time = parse_ts(market.get("close_time"))
    return bool(open_time and close_time and open_time <= now < close_time)


def market_is_stale(market: dict | None, now: dt.datetime) -> bool:
    if not market:
        return False
    close_time = parse_ts(market.get("close_time"))
    return bool(close_time and now >= close_time)


def quote_sides(orderbook: dict, candles: list[dict], minute: int) -> list[SideQuote]:
    book = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    yes_bid, yes_bid_size = best_book_level(book.get("yes_dollars") or book.get("yes"))
    no_bid, no_bid_size = best_book_level(book.get("no_dollars") or book.get("no"))

    raw = {
        "YES": {
            "bid": yes_bid,
            "bid_size": yes_bid_size,
            "ask": 1.0 - no_bid if no_bid is not None else None,
            "ask_size": no_bid_size,
        },
        "NO": {
            "bid": no_bid,
            "bid_size": no_bid_size,
            "ask": 1.0 - yes_bid if yes_bid is not None else None,
            "ask_size": yes_bid_size,
        },
    }

    out = []
    for side, values in raw.items():
        prior_asks = [side_ask_from_candle(c, side) for c in candles[:-1]]
        prior_asks = [x for x in prior_asks if x is not None]
        prior_peak = max(prior_asks) if prior_asks else None
        prior_low = min(prior_asks) if prior_asks else None
        prior_close = prior_asks[-1] if prior_asks else None
        last = side_candle_values(candles[-1], side) if candles else {}
        last_open = last.get("ask_open")
        last_close = last.get("ask_close")
        last_low = last.get("ask_low")
        last_high = last.get("ask_high")
        last_range = (
            last_high - last_low
            if last_high is not None and last_low is not None
            else None
        )
        last_body = (
            last_close - last_open
            if last_close is not None and last_open is not None
            else None
        )
        drop_from_prior_high = (
            prior_peak - last_close
            if prior_peak is not None and last_close is not None
            else None
        )
        ask = values["ask"]
        pullback = prior_peak - ask if prior_peak is not None and ask is not None else None
        out.append(
            SideQuote(
                side=side,
                bid=values["bid"],
                bid_size=values["bid_size"],
                ask=ask,
                ask_size=values["ask_size"],
                prior_peak_ask=prior_peak,
                prior_low_ask=prior_low,
                prior_close_ask=prior_close,
                last_ask_open=last_open,
                last_ask_close=last_close,
                last_ask_low=last_low,
                last_ask_high=last_high,
                last_range=last_range,
                last_body=last_body,
                drop_from_prior_high=drop_from_prior_high,
                pullback=pullback,
                live_match=False,
                strict_match=False,
                reclaim_signal=False,
                reclaim_name="",
            )
        )
    return out


def current_positions(raw_positions: dict, market_ticker: str, sides: list[SideQuote]) -> list[ActivePosition]:
    side_by_name = {side.side.lower(): side for side in sides}
    rows = []
    for pos in raw_positions.get("market_positions", []):
        if pos.get("ticker") != market_ticker:
            continue
        raw_count = pos.get("position_fp", pos.get("position", 0))
        try:
            signed_qty = int(round(float(raw_count)))
        except (TypeError, ValueError):
            signed_qty = 0
        if signed_qty == 0:
            continue

        side = "yes" if signed_qty > 0 else "no"
        qty = abs(signed_qty)
        quote = side_by_name.get(side)
        mark_cents = int(round(quote.bid * 100)) if quote and quote.bid is not None else None
        cost_cents = api_cents(pos, "market_exposure_dollars", "market_exposure")
        realized_cents = api_cents(pos, "realized_pnl_dollars", "realized_pnl")
        fees_cents = api_cents(pos, "fees_paid_dollars", "fees_paid")
        value_cents = mark_cents * qty if mark_cents is not None else None
        unrealized_cents = value_cents - cost_cents if value_cents is not None else None
        rows.append(
            ActivePosition(
                ticker=market_ticker,
                side=side.upper(),
                qty=qty,
                cost_cents=cost_cents,
                mark_cents=mark_cents,
                value_cents=value_cents,
                unrealized_cents=unrealized_cents,
                realized_cents=realized_cents,
                fees_cents=fees_cents,
            )
        )
    return rows


def current_orders(raw_orders: dict, market_ticker: str) -> list[ActiveOrder]:
    rows = []
    for order in raw_orders.get("orders", []):
        if order.get("ticker") != market_ticker:
            continue
        action, side, price_cents = order_intent(order)
        count = int(money(order.get("initial_count_fp", order.get("count_fp", order.get("count")))) or 0)
        remaining = int(
            money(
                order.get(
                    "remaining_count_fp",
                    order.get("remaining_count", order.get("count_fp", order.get("count"))),
                )
            )
            or 0
        )
        rows.append(
            ActiveOrder(
                order_id=str(order.get("order_id") or order.get("id") or ""),
                ticker=market_ticker,
                action=action.upper(),
                side=side.upper(),
                count=count,
                remaining_count=remaining,
                price_cents=price_cents,
                status=str(order.get("status") or ""),
                created_time=str(order.get("created_time") or order.get("created_ts") or ""),
            )
        )
    return rows


def position_sell_orders(pos: ActivePosition, orders: list[ActiveOrder]) -> list[ActiveOrder]:
    return [
        order
        for order in orders
        if order.ticker == pos.ticker
        and order.side == pos.side
        and order.action == "SELL"
        and order.remaining_count > 0
    ]


def exit_status(pos: ActivePosition, orders: list[ActiveOrder]) -> str:
    sell_orders = position_sell_orders(pos, orders)
    if sell_orders:
        parts = [
            f"{order.remaining_count}@{plain_cents(order.price_cents / 100 if order.price_cents is not None else None)}"
            for order in sell_orders
        ]
        return "STAGED " + ",".join(parts)
    if pos.mark_cents is not None and pos.mark_cents >= int(RECLAIM_TARGET * 100):
        return f"MISSING EXIT bid {pos.mark_cents}c"
    return f"MISSING EXIT target {plain_cents(RECLAIM_TARGET)}"


def active_market_state(
    client: KalshiClient,
    market_ticker: str,
    sides: list[SideQuote],
) -> tuple[list[ActivePosition], list[ActiveOrder], str | None]:
    try:
        positions = current_positions(client.positions(), market_ticker, sides)
        orders = current_orders(client.orders(), market_ticker)
        return positions, orders, None
    except Exception as exc:
        return [], [], str(exc)


def empty_trade_log() -> dict:
    return {
        "trades": [],
        "kalshi_fills": [],
        "kalshi_settlements": [],
        "kalshi_synced_at": None,
        "paper_trades": [],
        "paper_positions": {},
        "paper_entered_signals": {},
        "active_positions": {},
        "pending_orders": {},
        "entered_signals": {},
        "failed_entries": {},
    }


def load_trade_log(path: Path) -> dict:
    if not path.exists():
        return empty_trade_log()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty_trade_log()
    base = empty_trade_log()
    if isinstance(payload, dict):
        base.update(payload)
    for key, default in empty_trade_log().items():
        if not isinstance(base.get(key), type(default)):
            base[key] = default
    return base


def save_trade_log(path: Path, log: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(log, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def log_trade(log: dict, record: dict) -> None:
    log.setdefault("trades", []).append(record)


def trade_already_logged(log: dict, order_id: str) -> bool:
    return any(
        isinstance(record, dict) and str(record.get("order_id") or "") == order_id
        for record in log.get("trades", [])
    )


def btc15_rows(rows: list | object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("ticker") or row.get("market_ticker") or "").startswith(SERIES_TICKER)
    ]


def sync_kalshi_live_records(client: KalshiClient, log: dict, now: dt.datetime) -> None:
    synced_at = parse_ts(log.get("kalshi_synced_at"))
    if synced_at and (now - synced_at).total_seconds() < 60:
        return
    fills = btc15_rows(client.fills().get("fills", []))
    settlements = btc15_rows(client.settlements().get("settlements", []))
    log["kalshi_fills"] = sorted(fills, key=lambda row: str(row.get("created_time") or row.get("ts") or ""))
    log["kalshi_settlements"] = sorted(settlements, key=lambda row: str(row.get("settled_time") or ""))
    log["kalshi_synced_at"] = now.isoformat()


def settlement_pnl_cents(row: dict) -> int:
    yes_count = as_float(row.get("yes_count_fp"))
    no_count = as_float(row.get("no_count_fp"))
    yes_cost = as_float(row.get("yes_total_cost_dollars"))
    no_cost = as_float(row.get("no_total_cost_dollars"))
    fee = as_float(row.get("fee_cost"))
    result = str(row.get("market_result") or "").lower()
    payout = yes_count if result == "yes" else no_count if result == "no" else 0.0
    return int(round((payout - yes_cost - no_cost - fee) * 100))


def settlement_cost_cents(row: dict) -> int:
    return int(round((as_float(row.get("yes_total_cost_dollars")) + as_float(row.get("no_total_cost_dollars"))) * 100))


def settlement_fee_cents(row: dict) -> int:
    return int(round(as_float(row.get("fee_cost")) * 100))


def compute_settlement_stats(settlements: list[dict]) -> BotStats:
    closed_pnls = [settlement_pnl_cents(row) for row in settlements]
    closed_costs = [settlement_cost_cents(row) for row in settlements]
    fees = sum(settlement_fee_cents(row) for row in settlements)
    wins = sum(1 for pnl in closed_pnls if pnl > 0)
    losses = sum(1 for pnl in closed_pnls if pnl < 0)
    win_pnls = [pnl for pnl in closed_pnls if pnl > 0]
    loss_pnls = [pnl for pnl in closed_pnls if pnl < 0]
    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    return BotStats(
        closed=len(closed_pnls),
        wins=wins,
        losses=losses,
        realized_cents=sum(closed_pnls),
        total_cost_cents=sum(closed_costs),
        avg_win_cents=int(round(sum(win_pnls) / len(win_pnls))) if win_pnls else None,
        avg_loss_cents=int(round(sum(loss_pnls) / len(loss_pnls))) if loss_pnls else None,
        avg_roi_pct=(
            sum((pnl / cost) * 100 for pnl, cost in zip(closed_pnls, closed_costs) if cost > 0) / len(closed_costs)
            if closed_costs
            else None
        ),
        max_loss_cents=min(loss_pnls) if loss_pnls else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        total_fees_cents=fees,
        gross_loss_cents=gross_loss,
    )


def realized_trade_records(log: dict) -> list[dict]:
    pending_order_ids = set(log.get("pending_orders", {}).keys())
    rows = []
    for record in log.get("trades", []):
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        order_id = str(record.get("order_id") or "")
        action = str(record.get("action") or "").lower()
        if order_id.startswith("ERROR_") or metadata.get("error"):
            continue
        if action == "sell":
            if order_id in pending_order_ids:
                continue
            if metadata.get("submitted_only"):
                continue
        rows.append(record)
    return rows


def compute_stats(log: dict, positions: list[ActivePosition]) -> BotStats:
    settlements = btc15_rows(log.get("kalshi_settlements", []))
    if settlements:
        return compute_settlement_stats(settlements)

    grouped: dict[tuple[str, str], dict[str, object]] = {}
    now = utcnow()
    for record in realized_trade_records(log):
        ticker = str(record.get("ticker") or "")
        side = str(record.get("side") or "").lower()
        action = str(record.get("action") or "").lower()
        if not ticker.startswith(SERIES_TICKER) or side not in ("yes", "no"):
            continue
        key = (ticker, side)
        bucket = grouped.setdefault(key, {"buys": [], "sells": []})
        if action in ("buy", "sell"):
            bucket[f"{action}s"].append(record)

    closed_pnls: list[int] = []
    closed_costs: list[int] = []
    closed_keys = set()
    for key, bucket in grouped.items():
        buys = bucket["buys"]
        sells = bucket["sells"]
        if not buys or not sells:
            continue
        buy_cost = sum(int(buy.get("count") or 0) * int(buy.get("price_cents") or 0) for buy in buys)
        sell_value = sum(int(sell.get("count") or 0) * int(sell.get("price_cents") or 0) for sell in sells)
        buy_count = sum(int(buy.get("count") or 0) for buy in buys)
        sell_count = sum(int(sell.get("count") or 0) for sell in sells)
        if buy_count > 0 and sell_count >= buy_count:
            closed_pnls.append(sell_value - buy_cost)
            closed_costs.append(buy_cost)
            closed_keys.add(key)

    for info in log.get("pending_orders", {}).values():
        if not isinstance(info, dict) or info.get("type") != "sell":
            continue
        ticker = str(info.get("ticker") or "")
        side = str(info.get("side") or "").lower()
        key = (ticker, side)
        if key in closed_keys:
            continue
        close_time = market_close_from_ticker(ticker)
        if not close_time or now < close_time:
            continue
        bucket = grouped.get(key)
        if not bucket:
            continue
        buys = bucket["buys"]
        if not buys:
            continue
        buy_cost = sum(int(buy.get("count") or 0) * int(buy.get("price_cents") or 0) for buy in buys)
        if buy_cost > 0:
            closed_pnls.append(-buy_cost)
            closed_costs.append(buy_cost)
            closed_keys.add(key)

    wins = sum(1 for pnl in closed_pnls if pnl > 0)
    losses = sum(1 for pnl in closed_pnls if pnl < 0)
    realized_cents = sum(closed_pnls)
    win_pnls = [pnl for pnl in closed_pnls if pnl > 0]
    loss_pnls = [pnl for pnl in closed_pnls if pnl < 0]
    avg_win = int(round(sum(win_pnls) / len(win_pnls))) if win_pnls else None
    avg_loss = int(round(sum(loss_pnls) / len(loss_pnls))) if loss_pnls else None
    total_cost_cents = sum(closed_costs)
    avg_roi_pct = (
        sum((pnl / cost) * 100 for pnl, cost in zip(closed_pnls, closed_costs) if cost > 0) / len(closed_costs)
        if closed_costs
        else None
    )
    max_loss = min(loss_pnls) if loss_pnls else None
    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    return BotStats(
        closed=len(closed_pnls),
        wins=wins,
        losses=losses,
        realized_cents=realized_cents,
        total_cost_cents=total_cost_cents,
        avg_win_cents=avg_win,
        avg_loss_cents=avg_loss,
        avg_roi_pct=avg_roi_pct,
        max_loss_cents=max_loss,
        profit_factor=profit_factor,
        total_fees_cents=sum(pos.fees_cents or 0 for pos in positions),
        gross_loss_cents=gross_loss,
    )


def compute_paper_stats(
    log: dict,
    sides: list[SideQuote],
    starting_balance_cents: int,
) -> PaperAccountStats:
    closed_pnls: list[int] = []
    closed_costs: list[int] = []
    closed_fees = 0
    for record in log.get("paper_trades", []):
        if not isinstance(record, dict) or record.get("action") != "sell":
            continue
        pnl = record.get("net_pnl_cents")
        if pnl is None:
            continue
        closed_pnls.append(int(round(float(pnl))))
        closed_fees += int(round(float(record.get("fee_cents") or 0)))
        entry_cents = int(record.get("entry_cents") or 0)
        count = int(record.get("count") or 0)
        closed_costs.append(entry_cents * count)

    wins = sum(1 for pnl in closed_pnls if pnl > 0)
    losses = sum(1 for pnl in closed_pnls if pnl < 0)
    win_pnls = [pnl for pnl in closed_pnls if pnl > 0]
    loss_pnls = [pnl for pnl in closed_pnls if pnl < 0]
    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))

    open_positions = 0
    open_contracts = 0
    open_cost_cents = 0
    open_value_cents = 0
    open_fee_estimate = 0
    for pos in log.get("paper_positions", {}).values():
        if not isinstance(pos, dict):
            continue
        side = str(pos.get("side") or "").lower()
        quote = side_quote(sides, side)
        mark_cents = price_to_cents(quote.bid if quote else None)
        count = int(pos.get("count") or 0)
        entry_cents = int(pos.get("entry_cents") or 0)
        if count <= 0 or entry_cents <= 0:
            continue
        open_positions += 1
        open_contracts += count
        open_cost_cents += entry_cents * count
        if mark_cents is not None:
            open_value_cents += mark_cents * count
        target_cents = int(pos.get("target_cents") or PAPER_EXIT_TRIGGER_CENTS)
        open_fee_estimate += int(round(estimated_round_trip_fee_cents(entry_cents, target_cents, count)))

    open_unrealized = open_value_cents - open_cost_cents - open_fee_estimate
    realized = sum(closed_pnls)
    return PaperAccountStats(
        closed=len(closed_pnls),
        wins=wins,
        losses=losses,
        realized_cents=realized,
        open_positions=open_positions,
        open_contracts=open_contracts,
        open_cost_cents=open_cost_cents,
        open_value_cents=open_value_cents,
        open_unrealized_cents=open_unrealized,
        equity_cents=starting_balance_cents + realized + open_unrealized,
        total_fees_cents=closed_fees + open_fee_estimate,
        avg_win_cents=int(round(sum(win_pnls) / len(win_pnls))) if win_pnls else None,
        avg_loss_cents=int(round(sum(loss_pnls) / len(loss_pnls))) if loss_pnls else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
    )


def signal_key(signal: TradeSignal) -> str:
    return f"{signal.ticker}_{signal.side.lower()}_{signal.strategy}"


def pos_key(ticker: str, side: str) -> str:
    return f"{ticker}_{side.lower()}"


def selected_signals(
    ticker: str,
    count: int,
    reclaim_states: list[ReclaimState],
    entries_allowed: bool = True,
) -> list[TradeSignal]:
    if not entries_allowed:
        return []
    reasons = {
        RECLAIM_STRATEGY: "YES reclaim after minute 5; early low 15-30c; entry <=55c; BTC flat and $50-$150 below 15m EMA21",
        BTC_FADE_STRATEGY: "YES reclaim after a 25-30c washout while BTC moved $25-$75 against YES",
        NO_RECLAIM_STRATEGY: "NO reclaim from a 35-40c early low while BTC moved $25-$75 against NO",
    }
    return [
        TradeSignal(
            ticker=ticker,
            strategy=state.strategy,
            side=state.side.lower(),
            count=count,
            entry_cents=state.entry_cents,
            target_cents=state.target_cents,
            reason=reasons[state.strategy],
        )
        for state in reclaim_states
        if state.ready and state.entry_cents is not None
    ]


def risk_sized_contracts(
    balance: dict,
    entry_cents: int,
    fallback_contracts: int,
    risk_pct: float,
) -> int:
    if risk_pct <= 0:
        return fallback_contracts
    balance_cents = int(money(balance.get("balance")) or 0)
    risk_budget_cents = int(balance_cents * (risk_pct / 100))
    if entry_cents <= 0 or risk_budget_cents < entry_cents:
        return 0
    return math.ceil(risk_budget_cents / entry_cents)


def apply_signal_sizing(
    signals: list[TradeSignal],
    balance: dict,
    fallback_contracts: int,
    risk_pct: float,
) -> list[TradeSignal]:
    for signal in signals:
        signal.count = risk_sized_contracts(balance, signal.entry_cents, fallback_contracts, risk_pct)
    return signals


def paper_position_key(ticker: str, side: str, strategy: str) -> str:
    return f"{ticker}_{side.lower()}_{strategy}_paper"


def side_quote(sides: list[SideQuote], side: str) -> SideQuote | None:
    wanted = side.upper()
    return next((quote for quote in sides if quote.side == wanted), None)


def manage_paper_trades(
    log: dict,
    market_ticker: str,
    sides: list[SideQuote],
    signals: list[TradeSignal],
    now: dt.datetime,
) -> list[BotAction]:
    actions = []
    paper_positions = log.setdefault("paper_positions", {})
    paper_entered = log.setdefault("paper_entered_signals", {})
    paper_trades = log.setdefault("paper_trades", [])

    for key, pos in list(paper_positions.items()):
        if not isinstance(pos, dict) or pos.get("ticker") != market_ticker:
            continue
        side = str(pos.get("side") or "").lower()
        quote = side_quote(sides, side)
        exit_cents = price_to_cents(quote.bid if quote else None)
        target_cents = int(pos.get("target_cents") or PAPER_EXIT_TRIGGER_CENTS)
        if exit_cents is None or exit_cents < target_cents:
            continue
        count = int(pos.get("count") or 0)
        entry_cents = int(pos.get("entry_cents") or 0)
        gross_pnl = (exit_cents - entry_cents) * count
        fees = estimated_round_trip_fee_cents(entry_cents, exit_cents, count)
        net_pnl = gross_pnl - fees
        paper_trades.append(
            {
                "timestamp": now.isoformat(),
                "action": "sell",
                "ticker": market_ticker,
                "side": side,
                "count": count,
                "price_cents": exit_cents,
                "entry_cents": entry_cents,
                "gross_pnl_cents": gross_pnl,
                "fee_cents": fees,
                "net_pnl_cents": net_pnl,
                "strategy": pos.get("strategy"),
                "metadata": {"reason": f"paper exit bid >= {target_cents}c"},
            }
        )
        paper_positions.pop(key, None)
        actions.append(
            BotAction(
                "PAPER",
                f"SELL {count}x {display_strategy(str(pos.get('strategy') or ''))} {display_side(side)} @ {exit_cents}c net {dollars_from_cents(net_pnl)}",
            )
        )

    for signal in signals:
        key = paper_position_key(signal.ticker, signal.side, signal.strategy)
        if key in paper_positions or key in paper_entered:
            continue
        if signal.count <= 0:
            actions.append(BotAction("SKIP", f"{display_strategy(signal.strategy)} paper risk budget below 1 contract"))
            continue
        paper_positions[key] = {
            "ticker": signal.ticker,
            "side": signal.side,
            "strategy": signal.strategy,
            "count": signal.count,
            "entry_cents": signal.entry_cents,
            "target_cents": signal.target_cents,
            "entry_time": now.isoformat(),
        }
        paper_entered[key] = {
            "timestamp": now.isoformat(),
            "ticker": signal.ticker,
            "side": signal.side,
            "strategy": signal.strategy,
            "count": signal.count,
            "entry_cents": signal.entry_cents,
            "target_cents": signal.target_cents,
        }
        paper_trades.append(
            {
                "timestamp": now.isoformat(),
                "action": "buy",
                "ticker": signal.ticker,
                "side": signal.side,
                "count": signal.count,
                "price_cents": signal.entry_cents,
                "cost_cents": signal.count * signal.entry_cents,
                "strategy": signal.strategy,
                "metadata": {"reason": signal.reason, "paper": True},
            }
        )
        actions.append(
            BotAction(
                "PAPER",
                f"BUY {signal.count}x {display_strategy(signal.strategy)} {display_side(signal.side)} @ {signal.entry_cents}c; exit on bid >= {signal.target_cents}c",
            )
        )
    return actions


def position_qty(positions: list[ActivePosition], ticker: str, side: str) -> int:
    wanted = side.upper()
    return sum(pos.qty for pos in positions if pos.ticker == ticker and pos.side == wanted)


def has_resting_order(orders: list[ActiveOrder], ticker: str, side: str, action: str) -> bool:
    wanted_side = side.upper()
    wanted_action = action.upper()
    return any(
        order.ticker == ticker
        and order.side == wanted_side
        and order.action == wanted_action
        and order.remaining_count > 0
        for order in orders
    )


def active_position_for_signal(log: dict, signal: TradeSignal) -> dict | None:
    active = log.setdefault("active_positions", {})
    exact = active.get(signal_key(signal))
    if exact:
        return exact
    return active.get(pos_key(signal.ticker, signal.side))


def sync_open_positions(log: dict, positions: list[ActivePosition], now: dt.datetime) -> list[BotAction]:
    actions = []
    active = log.setdefault("active_positions", {})
    for pos in positions:
        side = pos.side.lower()
        has_record = any(
            info.get("ticker") == pos.ticker and str(info.get("side") or "").lower() == side
            for info in active.values()
            if isinstance(info, dict)
        )
        if has_record:
            continue
        active_key = pos_key(pos.ticker, side)
        avg_cost = int(round(pos.cost_cents / pos.qty)) if pos.qty else 0
        active[active_key] = {
            "ticker": pos.ticker,
            "side": side,
            "strategy": "ACCOUNT_SYNC",
            "entry_price_cents": avg_cost,
            "entry_count": pos.qty,
            "remaining_count": pos.qty,
            "target_cents": int(RECLAIM_TARGET * 100),
            "entry_time": None,
            "confirmed_at": now.isoformat(),
        }
        actions.append(BotAction("SYNC", f"tracking open {pos.qty}x {pos.ticker} {display_side(pos.side)} from account"))
    return actions


def entry_failure_cooldown(log: dict, key: str, now: dt.datetime) -> int:
    failed = log.setdefault("failed_entries", {})
    entry = failed.get(key)
    if not isinstance(entry, dict):
        return 0
    failed_at = parse_ts(entry.get("timestamp"))
    if not failed_at:
        failed.pop(key, None)
        return 0
    age = (now - failed_at).total_seconds()
    if age >= ENTRY_FAILURE_COOLDOWN_SECONDS:
        failed.pop(key, None)
        return 0
    return int(ENTRY_FAILURE_COOLDOWN_SECONDS - age)


def reconcile_pending_orders(
    client: KalshiClient,
    log: dict,
    market_ticker: str,
    positions: list[ActivePosition],
    orders: list[ActiveOrder],
    now: dt.datetime,
) -> list[BotAction]:
    actions = []
    pending = log.setdefault("pending_orders", {})
    if not pending:
        return actions

    resting_ids = {order.order_id: order for order in orders}
    # ActiveOrder.order_id is shortened for display, so also compare by prefix.
    resting_prefixes = {order.order_id for order in orders}
    to_remove = []

    for order_id, info in list(pending.items()):
        ticker = str(info.get("ticker") or "")
        if ticker != market_ticker:
            continue
        is_resting = order_id in resting_ids or any(order_id.startswith(prefix) or prefix.startswith(order_id) for prefix in resting_prefixes)
        side = str(info.get("side") or "").lower()
        qty = position_qty(positions, ticker, side)
        if info.get("type") == "buy":
            if is_resting:
                continue
            if qty > 0:
                active_key = str(info.get("signal_key") or f"{ticker}_{side}")
                log.setdefault("active_positions", {})[active_key] = {
                    "ticker": ticker,
                    "side": side,
                    "strategy": info.get("strategy"),
                    "entry_price_cents": info.get("price_cents"),
                    "entry_count": info.get("count"),
                    "remaining_count": qty,
                    "target_cents": info.get("target_cents"),
                    "entry_time": info.get("submitted_at"),
                    "confirmed_at": now.isoformat(),
                }
                actions.append(BotAction("FILLED", f"BUY confirmed {qty}x {ticker} {display_side(side)}"))
                to_remove.append(order_id)
            else:
                actions.append(BotAction("CLEARED", f"BUY order gone without position {ticker} {display_side(side)}"))
                to_remove.append(order_id)
        elif info.get("type") == "sell":
            if is_resting:
                continue
            active_key = str(info.get("signal_key") or f"{ticker}_{side}")
            if qty > 0:
                actions.append(BotAction("CLEARED", f"EXIT order gone; position still open {ticker} {display_side(side)}"))
            else:
                if order_id and not trade_already_logged(log, order_id):
                    count = int(info.get("count") or 0)
                    price_cents = int(info.get("price_cents") or 0)
                    log_trade(
                        log,
                        {
                            "timestamp": now.isoformat(),
                            "action": "sell",
                            "ticker": ticker,
                            "side": side,
                            "count": count,
                            "price_cents": price_cents,
                            "cost_cents": count * price_cents,
                            "order_id": order_id,
                            "strategy": info.get("strategy"),
                            "metadata": {"reason": "target exit", "signal_key": active_key, "filled": True},
                        },
                    )
                log.setdefault("active_positions", {}).pop(active_key, None)
                actions.append(BotAction("EXITED", f"SELL no longer resting {ticker} {display_side(side)}"))
            to_remove.append(order_id)

    for order_id in to_remove:
        pending.pop(order_id, None)
    return actions


def stage_missing_exits(
    client: KalshiClient,
    log: dict,
    positions: list[ActivePosition],
    orders: list[ActiveOrder],
    live: bool,
    now: dt.datetime,
) -> list[BotAction]:
    actions = []
    active = log.setdefault("active_positions", {})
    pending = log.setdefault("pending_orders", {})
    for active_key, info in list(active.items()):
        ticker = str(info.get("ticker") or "")
        side = str(info.get("side") or "").lower()
        target_cents = int(info.get("target_cents") or 0)
        if not ticker or side not in ("yes", "no") or target_cents <= 0:
            continue
        qty = position_qty(positions, ticker, side)
        if qty <= 0:
            active.pop(active_key, None)
            continue
        if has_resting_order(orders, ticker, side, "sell"):
            continue
        if any(
            pending_info.get("type") == "sell"
            and pending_info.get("ticker") == ticker
            and pending_info.get("side") == side
            for pending_info in pending.values()
        ):
            continue
        if not live:
            actions.append(BotAction("MONITOR", f"Would stage EXIT {qty}x {ticker} {display_side(side)} @ {target_cents}c"))
            continue
        try:
            result = client.create_order(ticker, "sell", side, qty, target_cents)
            order = result.get("order", result)
            order_id = str(order.get("order_id") or order.get("id") or "")
            pending[order_id] = {
                "type": "sell",
                "signal_key": active_key,
                "ticker": ticker,
                "side": side,
                "count": qty,
                "price_cents": target_cents,
                "target_cents": target_cents,
                "strategy": info.get("strategy"),
                "submitted_at": now.isoformat(),
            }
            actions.append(BotAction("LIVE", f"EXIT staged {qty}x {ticker} {display_side(side)} @ {target_cents}c"))
        except Exception as exc:
            log_trade(
                log,
                {
                    "timestamp": now.isoformat(),
                    "action": "sell",
                    "ticker": ticker,
                    "side": side,
                    "count": qty,
                    "price_cents": target_cents,
                    "cost_cents": qty * target_cents,
                    "order_id": f"ERROR_{int(time.time())}",
                    "strategy": info.get("strategy"),
                    "metadata": {"reason": "target exit", "signal_key": active_key, "error": str(exc)},
                },
            )
            actions.append(BotAction("ERROR", f"EXIT failed {ticker} {display_side(side)}: {exc}"))
    return actions


def place_entries(
    client: KalshiClient,
    log: dict,
    signals: list[TradeSignal],
    positions: list[ActivePosition],
    orders: list[ActiveOrder],
    live: bool,
    now: dt.datetime,
) -> list[BotAction]:
    actions = []
    pending = log.setdefault("pending_orders", {})
    entered = log.setdefault("entered_signals", {})
    for signal in signals:
        key = signal_key(signal)
        existing_qty = position_qty(positions, signal.ticker, signal.side)
        pending_qty = sum(
            int(info.get("count") or 0)
            for info in pending.values()
            if info.get("ticker") == signal.ticker
            and info.get("side") == signal.side
            and info.get("type") == "buy"
        )
        if key in entered or active_position_for_signal(log, signal):
            actions.append(BotAction("SKIP", f"{display_strategy(signal.strategy)} already entered {display_side(signal.side)}"))
            continue
        cooldown_left = entry_failure_cooldown(log, key, now)
        if cooldown_left > 0:
            actions.append(BotAction("SKIP", f"{display_strategy(signal.strategy)} {display_side(signal.side)} retry cooldown {cooldown_left}s"))
            continue
        if signal.count <= 0:
            actions.append(BotAction("SKIP", f"{display_strategy(signal.strategy)} {display_side(signal.side)} risk budget below 1 contract"))
            continue
        if has_resting_order(orders, signal.ticker, signal.side, "buy"):
            actions.append(BotAction("SKIP", f"resting BUY already exists {display_side(signal.side)}"))
            continue
        if not live:
            actions.append(
                BotAction(
                    "MONITOR",
                    f"Would BUY {signal.count}x {display_strategy(signal.strategy)} {display_side(signal.side)} @ {signal.entry_cents}c -> {signal.target_cents}c",
                )
            )
            continue
        record = {
            "timestamp": now.isoformat(),
            "action": "buy",
            "ticker": signal.ticker,
            "side": signal.side,
            "count": signal.count,
            "price_cents": signal.entry_cents,
            "cost_cents": signal.count * signal.entry_cents,
            "strategy": signal.strategy,
            "metadata": {"reason": signal.reason, "signal_key": key, "target_cents": signal.target_cents},
        }
        try:
            result = client.create_order(signal.ticker, "buy", signal.side, signal.count, signal.entry_cents)
            order = result.get("order", result)
            order_id = str(order.get("order_id") or order.get("id") or "")
            record["order_id"] = order_id
            pending[order_id] = {
                "type": "buy",
                "signal_key": key,
                "ticker": signal.ticker,
                "side": signal.side,
                "count": signal.count,
                "price_cents": signal.entry_cents,
                "target_cents": signal.target_cents,
                "strategy": signal.strategy,
                "submitted_at": now.isoformat(),
            }
            entered[key] = {
                "timestamp": now.isoformat(),
                "ticker": signal.ticker,
                "side": signal.side,
                "strategy": signal.strategy,
                "count": signal.count,
                "entry_cents": signal.entry_cents,
                "target_cents": signal.target_cents,
            }
            actions.append(
                BotAction(
                    "LIVE",
                    f"BUY sent {signal.count}x {display_strategy(signal.strategy)} {display_side(signal.side)} @ {signal.entry_cents}c",
                )
            )
        except Exception as exc:
            record["order_id"] = f"ERROR_{int(time.time())}"
            record["metadata"]["error"] = str(exc)
            log.setdefault("failed_entries", {})[key] = {
                "timestamp": now.isoformat(),
                "ticker": signal.ticker,
                "side": signal.side,
                "strategy": signal.strategy,
                "price_cents": signal.entry_cents,
                "error": str(exc),
            }
            actions.append(BotAction("ERROR", f"BUY failed {display_strategy(signal.strategy)} {display_side(signal.side)}: {exc}"))
        log_trade(log, record)
    return actions


def manage_orders(
    client: KalshiClient,
    log_path: Path,
    market_ticker: str,
    sides: list[SideQuote],
    positions: list[ActivePosition],
    orders: list[ActiveOrder],
    reclaim_states: list[ReclaimState],
    live: bool,
    paper: bool,
    contracts: int,
    risk_pct: float,
    balance: dict,
    now: dt.datetime,
    entries_allowed: bool,
    starting_balance_cents: int,
) -> tuple[list[BotAction], dict]:
    log = load_trade_log(log_path)
    try:
        sync_kalshi_live_records(client, log, now)
    except Exception as exc:
        actions = [BotAction("WAIT", f"Kalshi fill sync unavailable: {exc}")]
    else:
        actions = []
    actions.extend(sync_open_positions(log, positions, now))
    actions.extend(reconcile_pending_orders(client, log, market_ticker, positions, orders, now))
    actions.extend(stage_missing_exits(client, log, positions, orders, live, now))
    sizing_balance = balance
    if paper:
        paper_stats = compute_paper_stats(log, sides, starting_balance_cents)
        sizing_balance = {"balance": paper_stats.equity_cents}
    signals = apply_signal_sizing(
        selected_signals(market_ticker, contracts, reclaim_states, entries_allowed),
        sizing_balance,
        contracts,
        risk_pct,
    )
    if paper:
        actions.extend(manage_paper_trades(log, market_ticker, sides, signals, now))
        save_trade_log(log_path, log)
        return actions, log
    actions.extend(
        place_entries(
            client,
            log,
            signals,
            positions,
            orders,
            live,
            now,
        )
    )
    save_trade_log(log_path, log)
    return actions, log


def market_minute(market: dict, now: dt.datetime) -> int:
    open_time = parse_ts(market.get("open_time"))
    if not open_time:
        return 0
    return market_elapsed_seconds(market, now) // 60


def market_elapsed_seconds(market: dict, now: dt.datetime) -> int:
    open_time = parse_ts(market.get("open_time"))
    if not open_time:
        return 0
    return max(0, int((now - open_time).total_seconds()))


def signal_status(
    ready: bool,
    checks: list[tuple[str, bool]],
    armed_names: set[str],
    entries_allowed: bool = True,
) -> tuple[str, list[str], int, int]:
    passed_names = {name for name, ok in checks if ok}
    missing = [name for name, ok in checks if not ok]
    if not entries_allowed:
        return "WAITING", missing, len(passed_names), len(checks)
    if ready:
        return "SIGNAL", missing, len(passed_names), len(checks)
    if passed_names & armed_names:
        return "ARMED", missing, len(passed_names), len(checks)
    return "WAITING", missing, len(passed_names), len(checks)


def signal_status_rows(reclaim_states: list[ReclaimState], entries_allowed: bool = True) -> list[dict]:
    rows = []
    for state in reclaim_states:
        early_check = next((name for name, _ in state.checks if name.startswith("early_low")), "")
        status, missing, passed, total = signal_status(
            state.ready,
            state.checks,
            {"m6-10", early_check, "reclaim_m5"},
            entries_allowed,
        )
        if not state.btc_context.available and state.btc_context.error:
            missing = [*missing, state.btc_context.error]
        rows.append(
            {
                "side": display_side(state.side),
                "api_side": state.side.lower(),
                "strategy": state.strategy,
                "display_strategy": state.display_strategy,
                "status": status,
                "passed": passed,
                "total": total,
                "missing": missing,
                "ask": state.live_ask,
                "bid": None,
                "target_cents": state.target_cents,
            }
        )
    return rows


def signal_line(record: dict, ui: Ui) -> str:
    status_text = str(record["status"])
    status_color = Ui.GREEN if status_text == "SIGNAL" else Ui.YELLOW if status_text == "ARMED" else Ui.DIM
    return (
        f" {colored_side(str(record['side']), ui, 4)} {record['display_strategy']:<15} "
        f"{ui.s(status_text, status_color, Ui.BOLD if status_text == 'SIGNAL' else '')} "
        f"{record['passed']}/{record['total']}"
    )


def strategy_banner(parts: list[str], wait: str = "WAIT") -> str:
    return " | ".join(parts) if parts else wait


def fetch_candles(client: KalshiClient, market: dict, now: dt.datetime) -> list[dict]:
    ticker = market["ticker"]
    open_time = parse_ts(market.get("open_time")) or now - dt.timedelta(minutes=15)
    start_ts = int(open_time.timestamp())
    end_ts = int(now.timestamp())
    series_or_event = market.get("series_ticker") or SERIES_TICKER
    try:
        return client.candlesticks(series_or_event, ticker, start_ts, end_ts)
    except requests.HTTPError:
        event_ticker = market.get("event_ticker")
        if event_ticker and event_ticker != series_or_event:
            return client.candlesticks(event_ticker, ticker, start_ts, end_ts)
        raise


def fetch_coinbase_btc_candles(
    session: requests.Session,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict]:
    rows: dict[int, dict] = {}
    cursor = start.astimezone(dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    while cursor < end:
        chunk_end = min(cursor + dt.timedelta(minutes=300), end)
        response = session.get(
            COINBASE_CANDLES_URL,
            params={
                "granularity": 60,
                "start": cursor.isoformat().replace("+00:00", "Z"),
                "end": chunk_end.isoformat().replace("+00:00", "Z"),
            },
            timeout=10,
        )
        response.raise_for_status()
        for raw in response.json():
            if not isinstance(raw, list) or len(raw) < 6:
                continue
            ts = int(raw[0])
            rows[ts] = {
                "ts": dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc),
                "low": float(raw[1]),
                "high": float(raw[2]),
                "open": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]),
            }
        cursor = chunk_end
    return [rows[key] for key in sorted(rows)]


def ema(values: list[float], span: int) -> float | None:
    if not values:
        return None
    alpha = 2 / (span + 1)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
    return current


def btc_15m_closes(candles: list[dict], decision_time: dt.datetime) -> list[float]:
    closes_by_end: dict[dt.datetime, float] = {}
    for candle in candles:
        start = candle["ts"].astimezone(dt.timezone.utc)
        close_time = start + dt.timedelta(minutes=1)
        bucket_minute = (close_time.minute // 15) * 15
        bucket_end = close_time.replace(minute=bucket_minute, second=0, microsecond=0)
        if close_time.minute % 15:
            bucket_end += dt.timedelta(minutes=15)
        if bucket_end <= decision_time:
            closes_by_end[bucket_end] = float(candle["close"])
    return [closes_by_end[key] for key in sorted(closes_by_end)]


def build_btc_context(
    session: requests.Session,
    market: dict,
    now: dt.datetime,
) -> BtcContext:
    open_time = parse_ts(market.get("open_time"))
    if not open_time:
        return BtcContext(False, error="missing market open")
    decision_time = open_time + dt.timedelta(minutes=RECLAIM_DECISION_MINUTE)
    if now < decision_time:
        return BtcContext(False, error="waiting for minute 5 BTC context")
    try:
        candles = fetch_coinbase_btc_candles(
            session,
            open_time - dt.timedelta(hours=8),
            min(now, open_time + dt.timedelta(minutes=15)),
        )
    except Exception as exc:
        return BtcContext(False, error=f"BTC fetch failed: {exc}")

    first_window = [
        row
        for row in candles
        if open_time <= row["ts"] < decision_time
    ]
    decision_rows = [
        row
        for row in candles
        if open_time < row["ts"] + dt.timedelta(minutes=1) <= decision_time
    ]
    closes_15m = btc_15m_closes(candles, decision_time)
    btc15_ema21 = ema(closes_15m, 21)
    if not first_window or not decision_rows or btc15_ema21 is None:
        return BtcContext(False, error="insufficient BTC candles")

    btc_open_first = float(first_window[0]["open"])
    btc_close_decision = float(decision_rows[-1]["close"])
    signed_dist = btc_close_decision - btc15_ema21
    return BtcContext(
        True,
        btc_open_first=btc_open_first,
        btc_close_decision=btc_close_decision,
        btc_ret_window=btc_close_decision - btc_open_first,
        btc15_ema21_decision=btc15_ema21,
        signed_dist_ema21_15m=signed_dist,
    )


def side_candle_rows(candles: list[dict], side: str) -> list[dict]:
    rows = []
    for index, candle in enumerate(candles, start=1):
        values = side_candle_values(candle, side)
        rows.append({"minute": index, **values})
    return rows


def build_reclaim_state(
    market_ticker: str,
    sides: list[SideQuote],
    candles: list[dict],
    minute: int,
    btc_context: BtcContext,
    entries_allowed: bool,
) -> ReclaimState:
    return build_reclaim_states(
        market_ticker, sides, candles, minute, btc_context, entries_allowed
    )[0]


def build_reclaim_states(
    market_ticker: str,
    sides: list[SideQuote],
    candles: list[dict],
    minute: int,
    btc_context: BtcContext,
    entries_allowed: bool,
) -> list[ReclaimState]:
    configs = [
        (RECLAIM_STRATEGY, RECLAIM_DISPLAY, "YES", (RECLAIM_EARLY_LOW_MIN, 0.30), (0.35, 0.55), (-20.0, 20.0), (-150.0, -50.0), 70),
        (BTC_FADE_STRATEGY, BTC_FADE_DISPLAY, "YES", (0.25, 0.30), (0.45, 0.65), (-75.0, -25.0), None, 90),
        (NO_RECLAIM_STRATEGY, NO_RECLAIM_DISPLAY, "NO", (0.35, 0.40), (0.50, 0.70), (-75.0, -25.0), None, 80),
    ]
    states = []
    for strategy, display, side, early, entry, btc_ret, ema, target in configs:
        quote = next((row for row in sides if row.side == side), None)
        states.append(
            _build_configured_reclaim_state(
                strategy,
                display,
                side,
                quote,
                side_candle_rows(candles, side),
                minute,
                btc_context,
                entries_allowed,
                early,
                entry,
                btc_ret,
                ema,
                target,
            )
        )
    return states


def _build_configured_reclaim_state(
    strategy: str,
    display: str,
    side: str,
    quote: SideQuote | None,
    rows: list[dict],
    minute: int,
    btc_context: BtcContext,
    entries_allowed: bool,
    early_band: tuple[float, float],
    entry_band: tuple[float, float],
    btc_ret_band: tuple[float, float],
    ema_band: tuple[float, float] | None,
    target_cents: int,
) -> ReclaimState:
    early_rows = [row for row in rows[:RECLAIM_DECISION_MINUTE] if row.get("ask_low") is not None]
    decision_row = rows[RECLAIM_DECISION_MINUTE - 1] if len(rows) >= RECLAIM_DECISION_MINUTE else {}
    entry_row = rows[-1] if rows else {}
    signal_minute = int(entry_row.get("minute") or minute)

    early_low = min((float(row["ask_low"]) for row in early_rows), default=None)
    decision_close = decision_row.get("ask_close")
    entry_open = entry_row.get("ask_open")
    entry_close = entry_row.get("ask_close")
    live_ask = quote.ask if quote else None
    entry_cents = price_to_cents(live_ask)
    entry_body = (
        entry_close - entry_open
        if entry_open is not None and entry_close is not None
        else None
    )

    early_min, early_max = early_band
    entry_min, entry_max = entry_band
    ret_min, ret_max = btc_ret_band
    side_sign = 1 if side == "YES" else -1
    signed_btc_ret = side_sign * btc_context.btc_ret_window if btc_context.btc_ret_window is not None else None
    signed_ema_dist = side_sign * btc_context.signed_dist_ema21_15m if btc_context.signed_dist_ema21_15m is not None else None
    checks = [
        ("entries_on", entries_allowed),
        (side, quote is not None),
        ("m6-10", RECLAIM_ENTRY_MINUTE_MIN <= signal_minute <= RECLAIM_ENTRY_MINUTE_MAX),
        (
            f"early_low{int(early_min * 100)}-{int(early_max * 100)}",
            early_low is not None and early_min < early_low <= early_max,
        ),
        (
            f"close{int(entry_min * 100)}-{int(entry_max * 100)}",
            entry_close is not None and entry_min <= entry_close < entry_max,
        ),
        (
            f"ask<={int(entry_max * 100)}",
            live_ask is not None and entry_min <= live_ask <= entry_max,
        ),
        (
            "reclaim_m5",
            entry_close is not None
            and decision_close is not None
            and entry_close > decision_close,
        ),
        ("green", entry_body is not None and entry_body > 0),
        (
            f"btc_ret{int(ret_min)}:{int(ret_max)}",
            signed_btc_ret is not None and ret_min <= signed_btc_ret <= ret_max,
        ),
    ]
    if ema_band is not None:
        ema_min, ema_max = ema_band
        checks.append(("btc_below_ema50-150", signed_ema_dist is not None and ema_min <= signed_ema_dist < ema_max))
    ready = all(ok for _, ok in checks)
    if not btc_context.available:
        ready = False
    if entry_cents is None or entry_cents > int(entry_max * 100):
        ready = False

    return ReclaimState(
        strategy=strategy,
        display_strategy=display,
        side=side,
        minute=signal_minute,
        early_ask_low=early_low,
        decision_ask_close=decision_close,
        entry_ask_open=entry_open,
        entry_ask_close=entry_close,
        live_ask=live_ask,
        btc_context=btc_context,
        checks=checks,
        ready=ready,
        entry_cents=entry_cents,
        target_cents=target_cents,
    )


def print_snapshot(
    balance: dict,
    market: dict,
    sides: list[SideQuote],
    reclaim_states: list[ReclaimState],
    positions: list[ActivePosition],
    orders: list[ActiveOrder],
    stats: BotStats,
    paper_stats: PaperAccountStats,
    active_error: str | None,
    bot_actions: list[BotAction],
    live: bool,
    paper: bool,
    paper_positions: dict,
    contracts: int,
    risk_pct: float,
    starting_balance_cents: int,
    now: dt.datetime,
    color: bool = True,
) -> None:
    ui = Ui(color=color)
    open_time = parse_ts(market.get("open_time"))
    close_time = parse_ts(market.get("close_time"))
    seconds_left = int((close_time - now).total_seconds()) if close_time else 0
    seconds_left = max(0, seconds_left)
    entries_allowed = not close_time or seconds_left >= ENTRY_CUTOFF_SECONDS
    signal_rows = signal_status_rows(reclaim_states, entries_allowed)

    print("\033[2J\033[H", end="")
    live_cash_cents, _, live_total_cents = account_cents(balance)
    if paper:
        cash_cents = starting_balance_cents + paper_stats.realized_cents - paper_stats.open_cost_cents
        total_cents = paper_stats.equity_cents
    else:
        cash_cents = live_cash_cents
        total_cents = live_total_cents
    win_pct = (stats.wins / stats.closed * 100) if stats.closed else 0.0
    avg_win = dollars_from_cents(stats.avg_win_cents) if stats.avg_win_cents is not None else "--"
    avg_loss = dollars_from_cents(stats.avg_loss_cents) if stats.avg_loss_cents is not None else "--"
    account_pnl_cents = total_cents - starting_balance_cents
    account_profit_factor = (
        (stats.gross_loss_cents + account_pnl_cents) / stats.gross_loss_cents
        if stats.gross_loss_cents > 0
        else None
    )
    profit_factor = format_profit_factor(account_profit_factor, stats.wins, stats.losses)
    clock_left = format_clock(seconds_left) if close_time else "--:--"
    mode_color = Ui.GREEN if live else Ui.YELLOW if paper else Ui.DIM
    mode_label = "LIVE" if live else "PAPER" if paper else "MONITOR"
    sizing = f"risk {risk_pct:g}% cash/signal" if risk_pct > 0 else f"{contracts} contracts/signal"

    print(title_rule("Kalshi BTC Spreads Bot", ui))
    print(f"{'BTC 15 mins  | ' + clock_left + ' left':^{UI_WIDTH}}")
    mode_summary = f"{mode_label} | {sizing}"
    mode_padding = " " * max(0, (UI_WIDTH - len(mode_summary)) // 2)
    print(f"{mode_padding}{ui.s(mode_label, mode_color, Ui.BOLD if live else '')} | {sizing}")
    print(section_rule("Account", ui))
    print(f"{f'{dollars_from_cents(cash_cents)} cash | portfolio {dollars_from_cents(total_cents)}':^{UI_WIDTH}}")
    print(f"{f'P/L {format_account_pnl(total_cents, starting_balance_cents)}':^{UI_WIDTH}}")
    if paper:
        print(section_rule("Paper Stats", ui))
        paper_win_pct = (paper_stats.wins / paper_stats.closed * 100) if paper_stats.closed else 0.0
        paper_pf = format_profit_factor(paper_stats.profit_factor, paper_stats.wins, paper_stats.losses)
        paper_avg_win = dollars_from_cents(paper_stats.avg_win_cents) if paper_stats.avg_win_cents is not None else "--"
        paper_avg_loss = dollars_from_cents(paper_stats.avg_loss_cents) if paper_stats.avg_loss_cents is not None else "--"
        print(f"{f'{paper_stats.wins}W-{paper_stats.losses}L | win {paper_win_pct:.1f}% | Realized {dollars_from_cents(paper_stats.realized_cents)}':^{UI_WIDTH}}")
        print(f"{f'avg win {paper_avg_win} | avg loss {paper_avg_loss}':^{UI_WIDTH}}")
        print(f"{f'fees {dollars_from_cents(paper_stats.total_fees_cents)} | profit factor {paper_pf}':^{UI_WIDTH}}")
    elif live:
        print(section_rule("Live Stats", ui))
        print(f"{f'{stats.wins}W-{stats.losses}L | win {win_pct:.1f}%':^{UI_WIDTH}}")
        print(f"{f'avg win {avg_win} | avg loss {avg_loss}':^{UI_WIDTH}}")
        print(f"{f'fees {dollars_from_cents(stats.total_fees_cents)} | profit factor {profit_factor}':^{UI_WIDTH}}")
    print(section_rule("Signals", ui))
    for record in signal_rows:
        print(signal_line(record, ui))
    for action in bot_actions[-3:]:
        color_code = Ui.RED if action.status == "ERROR" else Ui.GREEN if action.status in ("LIVE", "FILLED", "EXITED", "PAPER") else Ui.DIM
        print(f"          {ui.s(action.status, color_code, Ui.BOLD if action.status in ('LIVE', 'ERROR') else ''):<8} {action.message}")
    print(section_rule("Quotes", ui))
    print(f"{'SIDE':<6}{'BID':^8}{'ASK':^8}{'SPRD':^8}{'BID_SIZE':^10}{'ASK_SIZE':^10}")
    for side in sides:
        print(
            f"{colored_side(side.side, ui, 6)}"
            f"{cents(side.bid):^8}"
            f"{cents(side.ask):^8}"
            f"{cents(quote_spread(side)):^8}"
            f"{(side.bid_size if side.bid_size is not None else 0):^10.2f}"
                f"{(side.ask_size if side.ask_size is not None else 0):^10.2f}"
        )
    if not paper:
        print(section_rule("Active Orders", ui))
        if active_error:
            print(f" Portfolio {ui.s('UNAVAILABLE', Ui.RED, Ui.BOLD)} | {active_error}")
        else:
            if positions:
                print("SIDE   QTY  FILL_PRICE  OPEN_COST  CURRENT  UPNL     FEES")
                for pos in positions:
                    fill_price_cents = (pos.cost_cents / pos.qty / 100) if pos.qty else None
                    print(
                        f"{colored_side(pos.side, ui, 5)} "
                        f"{pos.qty:>4} "
                        f"{plain_cents(fill_price_cents):>10} "
                        f"{dollars_from_cents(pos.cost_cents):>9} "
                        f"{dollars_from_cents(pos.value_cents):>8} "
                        f"{dollars_from_cents(pos.unrealized_cents):>8} "
                        f"{dollars_from_cents(pos.fees_cents):>8}"
                    )
            else:
                print(f" POS     {ui.s('none for current market', Ui.DIM)}")
            if orders:
                for order in orders:
                    label = "EXIT ORDER" if order.action == "SELL" else "ORDER"
                    print(f" {label:<10} ACTION SIDE  REM/COUNT  PRICE   STATUS")
                    print(
                        f" {order.order_id[:8]:<10} "
                        f"{colored_action(order.action, ui, 6)} "
                        f"{colored_side(order.side, ui, 4)} "
                        f"{order.remaining_count:>3}/{order.count:<3} "
                        f"{plain_cents(order.price_cents / 100 if order.price_cents is not None else None):>7} "
                        f"{order.status}"
                    )
            else:
                print(f" ORDER   {ui.s('none for current market', Ui.DIM)}")
    if not live:
        print(section_rule("Paper", ui))
        open_paper = [pos for pos in paper_positions.values() if isinstance(pos, dict)]
        if open_paper:
            print("SIDE   QTY  ENTRY  EXIT_TRIGGER  STRATEGY")
            for pos in open_paper:
                print(
                    f"{colored_side(str(pos.get('side') or ''), ui, 5)} "
                    f"{int(pos.get('count') or 0):>4} "
                    f"{plain_cents((int(pos.get('entry_cents') or 0)) / 100):>6} "
                    f"{int(pos.get('target_cents') or PAPER_EXIT_TRIGGER_CENTS):>11}c "
                    f"{display_strategy(str(pos.get('strategy') or ''))}"
                )
        else:
            print(f" PAPER   {ui.s('none open', Ui.DIM)}")


def write_snapshot(
    path: Path | None,
    balance: dict,
    market: dict,
    sides: list[SideQuote],
    reclaim_states: list[ReclaimState],
    positions: list[ActivePosition],
    orders: list[ActiveOrder],
    stats: BotStats,
    paper_stats: PaperAccountStats,
    active_error: str | None,
    bot_actions: list[BotAction],
    live: bool,
    paper: bool,
    paper_positions: dict,
    contracts: int,
    risk_pct: float,
    starting_balance_cents: int,
    now: dt.datetime,
) -> None:
    reclaim_state = reclaim_states[0] if reclaim_states else None
    if path is None:
        return
    open_time = parse_ts(market.get("open_time"))
    close_time = parse_ts(market.get("close_time"))
    live_cash_cents, live_position_value_cents, live_total_cents = account_cents(balance)
    if paper:
        cash_cents = starting_balance_cents + paper_stats.realized_cents - paper_stats.open_cost_cents
        position_value_cents = paper_stats.open_value_cents
        total_cents = paper_stats.equity_cents
    else:
        cash_cents = live_cash_cents
        position_value_cents = live_position_value_cents
        total_cents = live_total_cents
    account_pnl_cents = total_cents - starting_balance_cents
    minute = market_minute(market, now)
    seconds_left = int((close_time - now).total_seconds()) if close_time else ENTRY_CUTOFF_SECONDS
    entries_allowed = not close_time or seconds_left >= ENTRY_CUTOFF_SECONDS
    payload = {
        "updated_at": now.isoformat(),
        "balance_cents": cash_cents,
        "position_value_cents": position_value_cents,
        "portfolio_value_cents": total_cents,
        "starting_balance_cents": starting_balance_cents,
        "account_pnl_cents": account_pnl_cents,
        "account_pnl_pct": pct_from_start(total_cents, starting_balance_cents),
        "stats": {
            "closed": stats.closed,
            "wins": stats.wins,
            "losses": stats.losses,
            "win_pct": (stats.wins / stats.closed * 100) if stats.closed else 0.0,
            "realized_cents": stats.realized_cents,
            "account_pnl_cents": account_pnl_cents,
            "open_contracts": sum(pos.qty for pos in positions),
            "open_fees_cents": sum(pos.fees_cents or 0 for pos in positions),
            "total_cost_cents": stats.total_cost_cents,
            "avg_win_cents": stats.avg_win_cents,
            "avg_loss_cents": stats.avg_loss_cents,
            "avg_roi_pct": stats.avg_roi_pct,
            "max_loss_cents": stats.max_loss_cents,
            "profit_factor": stats.profit_factor,
        },
        "paper_stats": {
            "closed": paper_stats.closed,
            "wins": paper_stats.wins,
            "losses": paper_stats.losses,
            "win_pct": (paper_stats.wins / paper_stats.closed * 100) if paper_stats.closed else 0.0,
            "realized_cents": paper_stats.realized_cents,
            "open_positions": paper_stats.open_positions,
            "open_contracts": paper_stats.open_contracts,
            "open_cost_cents": paper_stats.open_cost_cents,
            "open_value_cents": paper_stats.open_value_cents,
            "equity_cents": paper_stats.equity_cents,
            "total_fees_cents": paper_stats.total_fees_cents,
            "avg_win_cents": paper_stats.avg_win_cents,
            "avg_loss_cents": paper_stats.avg_loss_cents,
            "profit_factor": paper_stats.profit_factor,
        },
        "market": {
            "ticker": market.get("ticker"),
            "event_ticker": market.get("event_ticker"),
            "series_ticker": market.get("series_ticker") or SERIES_TICKER,
            "title": market.get("title"),
            "open_time": open_time.isoformat() if open_time else None,
            "close_time": close_time.isoformat() if close_time else None,
            "minute": minute,
        },
        "rule": {
            "profile": "default_three_lane",
            "strategy": RECLAIM_STRATEGY,
            "display_strategy": RECLAIM_DISPLAY,
            "side": "yes",
            "decision_minute": RECLAIM_DECISION_MINUTE,
            "entry_minutes": [RECLAIM_ENTRY_MINUTE_MIN, RECLAIM_ENTRY_MINUTE_MAX],
            "early_ask_low": [RECLAIM_EARLY_LOW_MIN, RECLAIM_EARLY_LOW_MAX],
            "entry_ask": [RECLAIM_ENTRY_MIN, RECLAIM_ENTRY_MAX],
            "btc_first_5_return": [RECLAIM_BTC_RET_MIN, RECLAIM_BTC_RET_MAX],
            "btc_vs_15m_ema21": [RECLAIM_EMA_DIST_MIN, RECLAIM_EMA_DIST_MAX],
            "exit_target": RECLAIM_TARGET,
            "stop": None,
            "utc_hour_filter": None,
        },
        "sides": [
            {
                "side": display_side(side.side),
                "api_side": side.side.lower(),
                "bid": side.bid,
                "bid_size": side.bid_size,
                "ask": side.ask,
                "ask_size": side.ask_size,
                "spread": quote_spread(side),
                "prior_peak_ask": side.prior_peak_ask,
                "prior_low_ask": side.prior_low_ask,
                "prior_close_ask": side.prior_close_ask,
                "last_ask_open": side.last_ask_open,
                "last_ask_close": side.last_ask_close,
                "last_ask_low": side.last_ask_low,
                "last_ask_high": side.last_ask_high,
                "last_range": side.last_range,
                "last_body": side.last_body,
                "drop_from_prior_high": side.drop_from_prior_high,
                "pullback": side.pullback,
                "live_match": side.live_match,
                "strict_match": side.strict_match,
            }
            for side in sides
        ],
        "reclaim_state": None if reclaim_state is None else {
            "strategy": reclaim_state.strategy,
            "display_strategy": reclaim_state.display_strategy,
            "side": reclaim_state.side.lower(),
            "minute": reclaim_state.minute,
            "early_ask_low": reclaim_state.early_ask_low,
            "decision_ask_close": reclaim_state.decision_ask_close,
            "entry_ask_open": reclaim_state.entry_ask_open,
            "entry_ask_close": reclaim_state.entry_ask_close,
            "live_ask": reclaim_state.live_ask,
            "entry_cents": reclaim_state.entry_cents,
            "target_cents": reclaim_state.target_cents,
            "ready": reclaim_state.ready,
            "checks": dict(reclaim_state.checks),
            "btc": {
                "available": reclaim_state.btc_context.available,
                "btc_open_first": reclaim_state.btc_context.btc_open_first,
                "btc_close_decision": reclaim_state.btc_context.btc_close_decision,
                "btc_ret_window": reclaim_state.btc_context.btc_ret_window,
                "btc15_ema21_decision": reclaim_state.btc_context.btc15_ema21_decision,
                "signed_dist_ema21_15m": reclaim_state.btc_context.signed_dist_ema21_15m,
                "error": reclaim_state.btc_context.error,
            },
        },
        "signals": signal_status_rows(reclaim_states, entries_allowed),
        "active": {
            "error": active_error,
            "bot": {
                "live": live,
                "paper": paper,
                "contracts": contracts,
                "risk_pct": risk_pct,
                "actions": [
                    {"status": action.status, "message": action.message}
                    for action in bot_actions
                ],
            },
            "positions": [
                {
                    "ticker": pos.ticker,
                    "side": display_side(pos.side),
                    "api_side": pos.side.lower(),
                    "qty": pos.qty,
                    "cost_cents": pos.cost_cents,
                    "mark_cents": pos.mark_cents,
                    "value_cents": pos.value_cents,
                    "unrealized_cents": pos.unrealized_cents,
                    "realized_cents": pos.realized_cents,
                    "fees_cents": pos.fees_cents,
                    "exit_status": exit_status(pos, orders),
                    "exit_orders": [
                        {
                            "order_id": order.order_id,
                            "remaining_count": order.remaining_count,
                            "price_cents": order.price_cents,
                            "status": order.status,
                        }
                        for order in position_sell_orders(pos, orders)
                    ],
                }
                for pos in positions
            ],
            "orders": [
                {
                    "order_id": order.order_id,
                    "ticker": order.ticker,
                    "action": order.action,
                    "side": display_side(order.side),
                    "api_side": order.side.lower(),
                    "count": order.count,
                    "remaining_count": order.remaining_count,
                    "price_cents": order.price_cents,
                    "status": order.status,
                    "created_time": order.created_time,
                }
                for order in orders
            ],
            "paper_positions": paper_positions,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def build_client(args: argparse.Namespace) -> KalshiClient:
    load_dotenv(ROOT / ".env")
    return KalshiClient(
        base_url=args.base_url,
        api_key=os.getenv("KALSHI_API_KEY"),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH"),
    )


def run(args: argparse.Namespace) -> int:
    client = build_client(args)
    no_market_attempts = 0
    rollover_notice_printed = False
    balance_cache: tuple[dt.datetime, dict] | None = None
    market_cache: tuple[dt.datetime, list[dict]] | None = None
    candle_cache: dict[str, tuple[dt.datetime, list[dict]]] = {}
    candle_backoff_until: dict[str, dt.datetime] = {}
    btc_context_cache: dict[str, tuple[dt.datetime, BtcContext]] = {}
    last_ticker: str | None = None

    while True:
        try:
            now = utcnow()
            if balance_cache is None or (now - balance_cache[0]).total_seconds() >= 10:
                balance_cache = (now, client.balance())
            balance = balance_cache[1]
            cached_market = choose_current_market(market_cache[1], now) if market_cache else None
            market_cache_expired = market_cache is None or (now - market_cache[0]).total_seconds() >= 10
            if market_cache_expired or market_is_stale(cached_market, now):
                market_cache = (now, client.open_btc15_markets(now))
            markets = market_cache[1]
            market = choose_current_market(markets, now)
            if (not market or not market_is_current(market, now)) and in_rollover_wait(now):
                market_cache = (now, client.open_btc15_markets(now))
                markets = market_cache[1]
                market = choose_current_market(markets, now)
            if not market:
                if in_rollover_wait(now):
                    if not rollover_notice_printed:
                        print(f"{now.isoformat()} waiting for next {SERIES_TICKER} market at rollover")
                        rollover_notice_printed = True
                    if args.once:
                        return 0
                    time.sleep(args.interval)
                    continue
                rollover_notice_printed = False
                no_market_attempts += 1
                print(f"{now.isoformat()} no open {SERIES_TICKER} market found")
                if no_market_attempts >= NO_MARKET_RELOAD_ATTEMPTS:
                    print(
                        f"{utcnow().isoformat()} reloading Kalshi session after "
                        f"{no_market_attempts} missing-market attempts"
                    )
                    client = build_client(args)
                    no_market_attempts = 0
                    if args.once:
                        return 0
                    time.sleep(NO_MARKET_RELOAD_SLEEP_SECONDS)
            elif not market_is_current(market, now):
                open_time = parse_ts(market.get("open_time"))
                if open_time:
                    print(f"{now.isoformat()} waiting for {market.get('ticker')} to open at {open_time.isoformat()}")
                else:
                    print(f"{now.isoformat()} waiting for next {SERIES_TICKER} market")
                market_cache = None
            else:
                no_market_attempts = 0
                rollover_notice_printed = False
                ticker = market["ticker"]
                if last_ticker and last_ticker != ticker:
                    candle_cache.pop(last_ticker, None)
                    candle_backoff_until.pop(last_ticker, None)
                    btc_context_cache.pop(last_ticker, None)
                last_ticker = ticker
                orderbook = client.orderbook(ticker)
                cached_at, candles = candle_cache.get(ticker, (dt.datetime.min.replace(tzinfo=dt.timezone.utc), []))
                backoff_until = candle_backoff_until.get(ticker)
                cache_age = (now - cached_at).total_seconds()
                should_refresh_candles = cache_age >= CANDLE_REFRESH_SECONDS
                if backoff_until and now < backoff_until:
                    should_refresh_candles = False
                candle_notice: BotAction | None = None
                if should_refresh_candles:
                    try:
                        candles = fetch_candles(client, market, now)
                        candle_cache[ticker] = (now, candles)
                        candle_backoff_until.pop(ticker, None)
                    except requests.HTTPError as exc:
                        if exc.response is not None and exc.response.status_code == 429:
                            candle_backoff_until[ticker] = now + dt.timedelta(seconds=CANDLE_429_BACKOFF_SECONDS)
                            candle_notice = BotAction("WAIT", f"candles rate-limited; using {len(candles)} cached bars")
                        else:
                            raise
                minute = market_minute(market, now)
                sides = quote_sides(orderbook, candles, minute)
                btc_cached_at, btc_context = btc_context_cache.get(
                    ticker,
                    (dt.datetime.min.replace(tzinfo=dt.timezone.utc), BtcContext(False, error="BTC context not loaded")),
                )
                if (now - btc_cached_at).total_seconds() >= BTC_CONTEXT_REFRESH_SECONDS:
                    btc_context = build_btc_context(client.session, market, now)
                    btc_context_cache[ticker] = (now, btc_context)
                positions, orders, active_error = active_market_state(client, market["ticker"], sides)
                close_time = parse_ts(market.get("close_time"))
                seconds_left = int((close_time - now).total_seconds()) if close_time else ENTRY_CUTOFF_SECONDS
                entries_allowed = not close_time or seconds_left >= ENTRY_CUTOFF_SECONDS
                reclaim_states = build_reclaim_states(
                    market["ticker"],
                    sides,
                    candles,
                    minute,
                    btc_context,
                    entries_allowed,
                )
                bot_actions: list[BotAction] = []
                trade_log = load_trade_log(args.trade_log)
                if active_error is None:
                    bot_actions, trade_log = manage_orders(
                        client,
                        args.trade_log,
                        market["ticker"],
                        sides,
                        positions,
                        orders,
                        reclaim_states,
                        args.live,
                        args.paper,
                        args.contracts,
                        args.risk_pct,
                        balance,
                        now,
                        entries_allowed,
                        args.starting_balance_cents,
                    )
                stats = compute_stats(trade_log, positions)
                paper_stats = compute_paper_stats(trade_log, sides, args.starting_balance_cents)
                paper_positions = trade_log.get("paper_positions", {})
                if not isinstance(paper_positions, dict):
                    paper_positions = {}
                if candle_notice:
                    bot_actions.insert(0, candle_notice)
                elif backoff_until and now < backoff_until:
                    remaining = int((backoff_until - now).total_seconds())
                    bot_actions.insert(0, BotAction("WAIT", f"candles using cache; retry in {remaining}s"))
                print_snapshot(
                    balance,
                    market,
                    sides,
                    reclaim_states,
                    positions,
                    orders,
                    stats,
                    paper_stats,
                    active_error,
                    bot_actions,
                    args.live,
                    args.paper,
                    paper_positions,
                    args.contracts,
                    args.risk_pct,
                    args.starting_balance_cents,
                    now,
                    color=not args.no_color,
                )
                write_snapshot(
                    args.json_out,
                    balance,
                    market,
                    sides,
                    reclaim_states,
                    positions,
                    orders,
                    stats,
                    paper_stats,
                    active_error,
                    bot_actions,
                    args.live,
                    args.paper,
                    paper_positions,
                    args.contracts,
                    args.risk_pct,
                    args.starting_balance_cents,
                    now,
                )
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:
            print(f"{utcnow().isoformat()} monitor error: {exc}", file=sys.stderr)

        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live monitor for Kalshi BTC15 setup.")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between refreshes.")
    parser.add_argument("--once", action="store_true", help="Print one snapshot and exit.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Kalshi Trade API v2 base URL.")
    parser.add_argument("--live", action="store_true", help="Place live Kalshi orders for selected BTC15 strategies.")
    parser.add_argument("--paper", action="store_true", help="Simulate the default BTC15 strategies without sending Kalshi orders.")
    parser.add_argument(
        "--contracts",
        type=int,
        default=DEFAULT_ENTRY_CONTRACTS,
        help="Contracts per entry signal when --risk-pct is 0.",
    )
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=DEFAULT_RISK_PCT,
        help="Percent of cash balance to risk per entry signal. Use 0 for fixed --contracts sizing.",
    )
    parser.add_argument(
        "--starting-balance-cents",
        type=int,
        default=int(os.getenv("BTC15_STARTING_BALANCE_CENTS", DEFAULT_STARTING_BALANCE_CENTS)),
        help="Starting total account value in cents for account P/L percentage.",
    )
    parser.add_argument(
        "--trade-log",
        type=Path,
        default=DEFAULT_TRADE_LOG,
        help="Bot trade/order management log.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "reports" / "btc15_live_latest.json",
        help="Path to write the latest snapshot JSON. Use '' to disable.",
    )
    args = parser.parse_args()
    if args.live and args.paper:
        parser.error("--live and --paper cannot be used together")
    if args.json_out == Path(""):
        args.json_out = None
    args.contracts = max(1, args.contracts)
    args.risk_pct = max(0.0, args.risk_pct)
    args.starting_balance_cents = max(0, args.starting_balance_cents)
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
