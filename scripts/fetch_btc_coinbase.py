#!/usr/bin/env python3
"""Fetch Coinbase BTC-USD 1 minute candles for the Kalshi BTC15 date range."""

from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
KALSHI_MARKETS = ROOT / "data" / "raw" / "kalshi_btc15" / "live_markets.jsonl.gz"
OUT = ROOT / "data" / "external" / "btc_usd_1m_coinbase.parquet"
URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def load_market_range() -> tuple[pd.Timestamp, pd.Timestamp]:
    starts = []
    ends = []
    with gzip.open(KALSHI_MARKETS, "rt") as fh:
        for line in fh:
            obj = json.loads(line)
            for market in obj.get("response", {}).get("markets", []):
                if market.get("open_time") and market.get("close_time"):
                    starts.append(pd.Timestamp(market["open_time"]))
                    ends.append(pd.Timestamp(market["close_time"]))

    if not starts or not ends:
        raise RuntimeError(f"No market times found in {KALSHI_MARKETS}")

    start = min(starts) - pd.Timedelta(minutes=30)
    end = max(ends) + pd.Timedelta(minutes=5)
    return start, end


def fetch_chunk(session: requests.Session, start: pd.Timestamp, end: pd.Timestamp) -> list[list[float]]:
    params = {
        "granularity": 60,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    response = session.get(URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Coinbase error: {data}")
    return data


def fetch_range(start: pd.Timestamp, end: pd.Timestamp, sleep_seconds: float) -> pd.DataFrame:
    rows = []
    session = requests.Session()
    cursor = start.floor("min")
    max_span = pd.Timedelta(minutes=300)

    while cursor < end:
        chunk_end = min(cursor + max_span, end)
        chunk = fetch_chunk(session, cursor, chunk_end)
        rows.extend(chunk)
        print(f"fetched {cursor.isoformat()} -> {chunk_end.isoformat()} rows={len(chunk)}")
        cursor = chunk_end
        if sleep_seconds:
            time.sleep(sleep_seconds)

    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args()

    start, end = load_market_range()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = fetch_range(start, end, args.sleep)
    df.to_parquet(out, index=False)
    print(f"wrote {out.relative_to(ROOT)} rows={len(df):,}")
    print(f"range {df['ts'].min()} -> {df['ts'].max()}")


if __name__ == "__main__":
    main()
