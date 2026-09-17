#!/usr/bin/env python3
"""Estimate edge in Kalshi Miami hourly temperature markets.

This is read-only. It compares the live Miami hourly temperature ladder against
KMIA public weather observations and the NWS hourly forecast.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_JSON_OUT = ROOT / "reports" / "miami_temp_edge_latest.json"
KMIA_LAT = 25.7959
KMIA_LON = -80.2870
MIAMI_TEMP_SERIES = "KXTEMPMIAH"


@dataclass
class TempMarket:
    ticker: str
    event_ticker: str
    title: str
    close_time: dt.datetime
    threshold_f: float
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    volume: float
    open_interest: float


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(value: object) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def fnum(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def c_to_f(value_c: float) -> float:
    return value_c * 9.0 / 5.0 + 32.0


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-weather-edge/0.1"})

    def get_json(self, url_or_path: str, params: dict[str, object] | None = None) -> dict | list:
        query = "?" + urlencode(params, doseq=True) if params else ""
        url = url_or_path if url_or_path.startswith("http") else self.base_url + url_or_path
        response = self.session.get(url + query, timeout=20)
        response.raise_for_status()
        return response.json()


def fetch_miami_temp_markets(client: HttpClient) -> list[TempMarket]:
    payload = client.get_json("/markets", {"series_ticker": MIAMI_TEMP_SERIES, "status": "open", "limit": 1000})
    markets = []
    for row in payload.get("markets") or []:
        title = str(row.get("title") or "")
        if "miami" not in title.lower() or "temp" not in title.lower():
            continue
        threshold = parse_threshold(title)
        close_time = parse_ts(row.get("close_time") or row.get("expected_expiration_time"))
        if threshold is None or close_time is None:
            continue
        markets.append(
            TempMarket(
                ticker=str(row.get("ticker") or ""),
                event_ticker=str(row.get("event_ticker") or ""),
                title=title,
                close_time=close_time,
                threshold_f=threshold,
                yes_bid=fnum(row.get("yes_bid_dollars") or row.get("yes_bid")),
                yes_ask=fnum(row.get("yes_ask_dollars") or row.get("yes_ask")),
                no_bid=fnum(row.get("no_bid_dollars") or row.get("no_bid")),
                no_ask=fnum(row.get("no_ask_dollars") or row.get("no_ask")),
                volume=fnum(row.get("volume_fp") or row.get("volume")) or 0.0,
                open_interest=fnum(row.get("open_interest_fp") or row.get("open_interest")) or 0.0,
            )
        )
    markets.sort(key=lambda market: (market.close_time, market.threshold_f))
    return markets


def parse_threshold(title: str) -> float | None:
    match = re.search(r"above\s+(-?\d+(?:\.\d+)?)", title, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def fetch_kmia_weather(client: HttpClient) -> dict:
    metars = client.get_json("https://aviationweather.gov/api/data/metar", {"ids": "KMIA", "format": "json", "taf": "true", "hours": 3})
    latest_metar = metars[0] if isinstance(metars, list) and metars else {}
    nws_latest = client.get_json("https://api.weather.gov/stations/KMIA/observations/latest")
    points = client.get_json(f"https://api.weather.gov/points/{KMIA_LAT},{KMIA_LON}")
    hourly_url = points["properties"]["forecastHourly"]
    hourly = client.get_json(hourly_url)
    return {"aviation_latest": latest_metar, "nws_latest": nws_latest, "nws_hourly": hourly}


def observation_f(weather: dict) -> float | None:
    nws_temp_c = (((weather.get("nws_latest") or {}).get("properties") or {}).get("temperature") or {}).get("value")
    if nws_temp_c is not None:
        return c_to_f(float(nws_temp_c))
    aviation_temp_c = (weather.get("aviation_latest") or {}).get("temp")
    if aviation_temp_c is not None:
        return c_to_f(float(aviation_temp_c))
    return None


def latest_observation_time(weather: dict) -> str:
    props = ((weather.get("nws_latest") or {}).get("properties") or {})
    return str(props.get("timestamp") or (weather.get("aviation_latest") or {}).get("reportTime") or "")


def forecast_for_close(weather: dict, close_time: dt.datetime) -> dict | None:
    periods = (((weather.get("nws_hourly") or {}).get("properties") or {}).get("periods") or [])
    for period in periods:
        start = parse_ts(period.get("startTime"))
        end = parse_ts(period.get("endTime"))
        if start and end and start <= close_time < end:
            return period
    return None


def estimate_target_temp_f(observed_f: float | None, forecast_period: dict | None, close_time: dt.datetime, now: dt.datetime) -> tuple[float | None, float]:
    forecast_f = fnum((forecast_period or {}).get("temperature"))
    if observed_f is None:
        return forecast_f, 1.6
    if forecast_f is None:
        return observed_f, 1.2

    minutes_left = max(0.0, (close_time - now).total_seconds() / 60.0)
    forecast_weight = min(0.65, max(0.20, minutes_left / 60.0))
    mean = observed_f * (1.0 - forecast_weight) + forecast_f * forecast_weight
    sigma = max(0.65, min(1.8, 0.55 + minutes_left / 45.0))
    return mean, sigma


def score_market(market: TempMarket, fair_yes: float) -> dict:
    fair_no = 1.0 - fair_yes
    yes_edge = fair_yes - market.yes_ask if market.yes_ask is not None else None
    no_edge = fair_no - market.no_ask if market.no_ask is not None else None
    if yes_edge is None and no_edge is None:
        side = "WAIT"
        edge = None
    elif no_edge is None or (yes_edge is not None and yes_edge >= no_edge):
        side = "YES" if yes_edge and yes_edge > 0 else "WAIT"
        edge = yes_edge
    else:
        side = "NO" if no_edge and no_edge > 0 else "WAIT"
        edge = no_edge
    return {
        "ticker": market.ticker,
        "title": market.title,
        "close_time": market.close_time.isoformat(),
        "threshold_f": market.threshold_f,
        "fair_yes": round(fair_yes, 4),
        "fair_no": round(fair_no, 4),
        "yes_bid": market.yes_bid,
        "yes_ask": market.yes_ask,
        "no_bid": market.no_bid,
        "no_ask": market.no_ask,
        "yes_edge": round(yes_edge, 4) if yes_edge is not None else None,
        "no_edge": round(no_edge, 4) if no_edge is not None else None,
        "best_side": side,
        "best_edge": round(edge, 4) if edge is not None else None,
        "volume": market.volume,
        "open_interest": market.open_interest,
    }


def build_report(client: HttpClient) -> dict:
    now = utcnow()
    markets = fetch_miami_temp_markets(client)
    weather = fetch_kmia_weather(client)
    observed_f = observation_f(weather)

    rows = []
    for market in markets:
        period = forecast_for_close(weather, market.close_time)
        mean_f, sigma_f = estimate_target_temp_f(observed_f, period, market.close_time, now)
        if mean_f is None:
            continue
        fair_yes = 1.0 - normal_cdf(market.threshold_f, mean_f, sigma_f)
        scored = score_market(market, fair_yes)
        scored["model_mean_f"] = round(mean_f, 2)
        scored["model_sigma_f"] = round(sigma_f, 2)
        scored["nws_hourly_forecast_f"] = (period or {}).get("temperature")
        scored["nws_short_forecast"] = (period or {}).get("shortForecast")
        rows.append(scored)

    rows.sort(key=lambda row: (-(row["best_edge"] or -99), row["close_time"], row["threshold_f"]))
    return {
        "updated_at": now.isoformat(),
        "market_series": MIAMI_TEMP_SERIES,
        "station": "KMIA",
        "observed_f": round(observed_f, 2) if observed_f is not None else None,
        "observed_at": latest_observation_time(weather),
        "notes": [
            "Fair value is a simple normal model using KMIA observation plus NWS hourly forecast.",
            "Kalshi hourly temperature rules may settle from The Weather Company, so treat NWS/KMIA as a proxy.",
            "Positive edge means estimated fair probability minus current ask.",
        ],
        "markets": rows,
    }


def pct(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:5.1f}%"


def price(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:5.1f}c"


def print_report(report: dict, min_edge: float, limit: int) -> None:
    print(f"Miami temp edge @ {report['updated_at']}")
    print(f"Observed KMIA: {report['observed_f']}F at {report['observed_at']}")
    print()
    shown = 0
    for row in report["markets"]:
        if shown >= limit:
            break
        if row["best_edge"] is not None and row["best_edge"] < min_edge:
            continue
        shown += 1
        print(
            f"{row['best_side']:>4} edge {pct(row['best_edge'])} | "
            f"fair YES {pct(row['fair_yes'])} | "
            f"YES {price(row['yes_bid'])}/{price(row['yes_ask'])} | "
            f"NO {price(row['no_bid'])}/{price(row['no_ask'])} | "
            f">{row['threshold_f']:.2f}F | mean {row['model_mean_f']}F sigma {row['model_sigma_f']} | "
            f"vol {row['volume']:.2f}"
        )
        print(f"     {row['ticker']} | {row['title']}")
    if shown == 0:
        print("No markets met the edge filter.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Miami temperature edge scanner.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Kalshi Trade API v2 base URL.")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT, help="Output JSON report path.")
    parser.add_argument("--min-edge", type=float, default=0.03, help="Minimum edge to print, as decimal probability.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum rows to print.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = HttpClient(args.base_url)
    report = build_report(client)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print_report(report, args.min_edge, args.limit)
    print(f"Wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
