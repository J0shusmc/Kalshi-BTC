#!/usr/bin/env python3
"""Standalone Florida weather market scanner for Kalshi.

This is intentionally separate from the BTC15 bot. It only reads public Kalshi
market data and writes a compact report that can be embedded in a combined
dashboard later.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_JSON_OUT = ROOT / "reports" / "florida_weather_markets_latest.json"

FLORIDA_TERMS = ("florida", "miami", "tampa", "orlando", "jacksonville", "gulf coast", "fla")
CITY_TERMS = ("miami", "tampa", "orlando", "jacksonville")
REGIONAL_TERMS = ("florida", "gulf coast", "coastal")
WEATHER_TERMS = (
    "weather",
    "temperature",
    "temp",
    "high",
    "low",
    "rain",
    "precipitation",
    "snow",
    "storm",
    "tornado",
    "natural disaster",
)
SKIP_CATEGORIES = {"sports", "crypto", "financials"}
SKIP_TERMS = ("football", "baseball", "basketball", "soccer", "stock", "nasdaq", "s&p", "bitcoin", "crypto")

KNOWN_FLORIDA_SERIES = (
    "KXHIGHMIA",
    "KXLOWTMIA",
    "KXTEMPMIAH",
    "KXRAINMIAM",
    "KXRAINMIA",
    "KXHURMIA",
    "KXHURTB",
    "KXHURORL",
    "KXHURJACKFL",
    "KXHURCATFL",
    "KXHURPATHFLA",
    "KXHURPATHGULFCOAST",
    "KXHURPATHGENERAL",
    "KXHURPATHGENERALMAJOR",
    "KXFIRSTHURRICANE",
    "KXNEXTHURDATE",
    "KXNEXTCAT5HURDATE",
    "KXHURCTOT",
    "KXHURCTOTMAJ",
    "KXTROPSTORM",
    "KXNAMEDSTORM",
    "KXTORNADO",
)


@dataclass
class MarketRow:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    category: str
    close_time: str
    yes_bid: str
    yes_ask: str
    no_bid: str
    no_ask: str
    volume: str
    open_interest: str
    liquidity: str
    match_type: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


class KalshiPublicClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def get(self, path: str, params: dict[str, object] | None = None) -> dict:
        query = "?" + urlencode(params, doseq=True) if params else ""
        response = self.session.get(self.base_url + path + query, timeout=20)
        response.raise_for_status()
        return response.json()

    def paged(self, path: str, params: dict[str, object] | None = None, max_pages: int = 10) -> list[dict]:
        params = dict(params or {})
        params.setdefault("limit", 1000)
        rows: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            if cursor:
                params["cursor"] = cursor
            payload = self.get(path, params)
            key = "markets" if "markets" in payload else "series"
            rows.extend(payload.get(key) or [])
            cursor = payload.get("cursor")
            if not cursor:
                break
        return rows


def text_blob(row: dict) -> str:
    parts = [
        row.get("ticker"),
        row.get("event_ticker"),
        row.get("title"),
        row.get("subtitle"),
        row.get("yes_sub_title"),
        row.get("no_sub_title"),
        row.get("category"),
        row.get("tags"),
        row.get("product_metadata"),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def is_weather(row: dict) -> bool:
    category = str(row.get("category") or "").lower()
    blob = text_blob(row)
    if category in SKIP_CATEGORIES or any(term in blob for term in SKIP_TERMS):
        return False
    return category == "climate and weather" or any(term in blob for term in WEATHER_TERMS)


def classify_match(row: dict) -> str | None:
    blob = text_blob(row)
    if not any(term in blob for term in FLORIDA_TERMS):
        return None
    if any(term in blob for term in CITY_TERMS):
        return "florida_city"
    if any(term in blob for term in REGIONAL_TERMS):
        return "florida_regional"
    if "hurricane" in blob or "tropical storm" in blob or "named storm" in blob:
        return "hurricane_basin"
    return None


def cents(value: object) -> str:
    if value in (None, ""):
        return "--"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{amount * 100:.1f}c"


def market_row(market: dict, match_type: str) -> MarketRow:
    subtitle = str(market.get("subtitle") or market.get("yes_sub_title") or "")
    if subtitle.startswith("-1"):
        subtitle = ""
    return MarketRow(
        ticker=str(market.get("ticker") or ""),
        event_ticker=str(market.get("event_ticker") or ""),
        title=str(market.get("title") or ""),
        subtitle=subtitle,
        category=str(market.get("category") or ""),
        close_time=str(market.get("close_time") or market.get("expected_expiration_time") or ""),
        yes_bid=cents(market.get("yes_bid_dollars") or market.get("yes_bid")),
        yes_ask=cents(market.get("yes_ask_dollars") or market.get("yes_ask")),
        no_bid=cents(market.get("no_bid_dollars") or market.get("no_bid")),
        no_ask=cents(market.get("no_ask_dollars") or market.get("no_ask")),
        volume=str(market.get("volume_fp") or market.get("volume") or "0"),
        open_interest=str(market.get("open_interest_fp") or market.get("open_interest") or "0"),
        liquidity=str(market.get("liquidity_dollars") or ""),
        match_type=match_type,
    )


def collect_markets(client: KalshiPublicClient, max_pages: int) -> tuple[list[dict], list[dict]]:
    all_markets = client.paged("/markets", {"status": "open", "mve_filter": "exclude"}, max_pages=max_pages)

    known_markets: list[dict] = []
    seen = {m.get("ticker") for m in all_markets}
    for series_ticker in KNOWN_FLORIDA_SERIES:
        for status in ("open", "active"):
            try:
                payload = client.get("/markets", {"series_ticker": series_ticker, "status": status, "limit": 1000})
            except requests.RequestException:
                continue
            for market in payload.get("markets") or []:
                if market.get("ticker") not in seen:
                    known_markets.append(market)
                    seen.add(market.get("ticker"))

    return all_markets, known_markets


def build_report(client: KalshiPublicClient, max_pages: int) -> dict:
    open_markets, known_markets = collect_markets(client, max_pages)
    matches: list[MarketRow] = []
    for market in [*open_markets, *known_markets]:
        if not is_weather(market):
            continue
        match_type = classify_match(market)
        if match_type:
            matches.append(market_row(market, match_type))

    order = {"florida_city": 0, "florida_regional": 1, "hurricane_basin": 2}
    matches.sort(key=lambda row: (order.get(row.match_type, 9), row.close_time, row.ticker))

    return {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "Florida weather first; city, regional, and hurricane-basin markets separated.",
        "base_url": client.base_url,
        "counts": {
            "florida_city": sum(1 for row in matches if row.match_type == "florida_city"),
            "florida_regional": sum(1 for row in matches if row.match_type == "florida_regional"),
            "hurricane_basin": sum(1 for row in matches if row.match_type == "hurricane_basin"),
            "open_markets_scanned": len(open_markets),
            "known_florida_series": len(KNOWN_FLORIDA_SERIES),
        },
        "markets": [row.as_dict() for row in matches],
    }


def print_report(report: dict, limit: int) -> None:
    counts = report["counts"]
    print(f"Florida weather scan @ {report['updated_at']}")
    print(
        "Matches: "
        f"city={counts['florida_city']} | regional={counts['florida_regional']} | "
        f"hurricane_basin={counts['hurricane_basin']} | scanned={counts['open_markets_scanned']}"
    )
    print()
    for match_type in ("florida_city", "florida_regional", "hurricane_basin"):
        rows = [row for row in report["markets"] if row["match_type"] == match_type][:limit]
        print(match_type.replace("_", " ").title())
        if not rows:
            print("  none")
            continue
        for row in rows:
            title = row["title"]
            if row["subtitle"]:
                title = f"{title} - {row['subtitle']}"
            print(
                f"  {row['ticker']} | YES {row['yes_bid']}/{row['yes_ask']} | "
                f"NO {row['no_bid']}/{row['no_ask']} | vol {row['volume']} | {title}"
            )
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Florida Kalshi weather market scanner.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Kalshi Trade API v2 base URL.")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT, help="Output JSON report path.")
    parser.add_argument("--max-pages", type=int, default=10, help="Open-market pages to scan.")
    parser.add_argument("--limit", type=int, default=12, help="Rows to print per match group.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = KalshiPublicClient(args.base_url)
    report = build_report(client, args.max_pages)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print_report(report, args.limit)
    print(f"Wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
