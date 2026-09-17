#!/usr/bin/env python3
"""Research BTC15 first-10-minute dips that rebound to 60c+.

This intentionally ignores settlement and 99c exits. The question is whether
YES/NO contracts that dip into the 20s during the first 10 minutes later offer
a 60c+ bid for a spread-style exit.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CANDLES_DIR = ROOT / "data" / "raw" / "kalshi_btc15" / "candles_by_ticker"
TRAINING = ROOT / "data" / "kalshi_btc15_t600_training.parquet"
REPORTS = ROOT / "reports"

DIP_MIN = 0.20
DIP_MAX = 0.30
REBOUND_BID = 0.60
MAX_DIP_MINUTE = 10


def num(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def price(node: dict, key: str) -> float:
    return num(node.get(f"{key}_dollars", node.get(key)))


def side_prices(candle: dict, side: str) -> dict[str, float]:
    yes_ask = candle.get("yes_ask", {}) or {}
    yes_bid = candle.get("yes_bid", {}) or {}
    if side == "YES":
        ask_open = price(yes_ask, "open")
        ask_close = price(yes_ask, "close")
        ask_low = price(yes_ask, "low")
        ask_high = price(yes_ask, "high")
        bid_open = price(yes_bid, "open")
        bid_close = price(yes_bid, "close")
        bid_low = price(yes_bid, "low")
        bid_high = price(yes_bid, "high")
    else:
        ask_open = 1.0 - price(yes_bid, "open")
        ask_close = 1.0 - price(yes_bid, "close")
        ask_low = 1.0 - price(yes_bid, "high")
        ask_high = 1.0 - price(yes_bid, "low")
        bid_open = 1.0 - price(yes_ask, "open")
        bid_close = 1.0 - price(yes_ask, "close")
        bid_low = 1.0 - price(yes_ask, "high")
        bid_high = 1.0 - price(yes_ask, "low")

    return {
        "ask_open": ask_open,
        "ask_close": ask_close,
        "ask_low": ask_low,
        "ask_high": ask_high,
        "bid_open": bid_open,
        "bid_close": bid_close,
        "bid_low": bid_low,
        "bid_high": bid_high,
        "spread_close": ask_close - bid_close,
        "spread_low_proxy": ask_low - bid_high,
        "volume": num(candle.get("volume_fp", candle.get("volume"))),
        "open_interest": num(candle.get("open_interest_fp", candle.get("open_interest"))),
    }


def load_candles(ticker: str) -> list[dict]:
    path = CANDLES_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    with path.open() as fh:
        return json.load(fh).get("response", {}).get("candlesticks", [])


def scan(mode: str) -> pd.DataFrame:
    markets = pd.read_parquet(TRAINING)[["kalshi_ticker", "market_start", "market_end"]]
    rows = []
    for market in markets.itertuples(index=False):
        candles = load_candles(market.kalshi_ticker)
        if len(candles) < MAX_DIP_MINUTE:
            continue

        for side in ("YES", "NO"):
            path = []
            for i, candle in enumerate(candles):
                vals = side_prices(candle, side)
                vals["minute"] = i + 1
                path.append(vals)

            if mode == "close":
                dip_indices = [
                    i
                    for i, point in enumerate(path[:MAX_DIP_MINUTE])
                    if DIP_MIN <= point["ask_close"] < DIP_MAX
                ]
                dip_price_col = "ask_close"
            elif mode == "touch":
                dip_indices = [
                    i
                    for i, point in enumerate(path[:MAX_DIP_MINUTE])
                    if DIP_MIN <= point["ask_low"] < DIP_MAX
                ]
                dip_price_col = "ask_low"
            else:
                raise ValueError(f"unknown mode: {mode}")

            if not dip_indices:
                continue

            # Live-feasible path: take the first qualifying dip for this side.
            i = dip_indices[0]
            dip = path[i]
            future = path[i + 1 :]
            future_bid_highs = [x["bid_high"] for x in future if not np.isnan(x["bid_high"])]
            future_ask_highs = [x["ask_high"] for x in future if not np.isnan(x["ask_high"])]
            hit60_bid = bool(future_bid_highs and max(future_bid_highs) >= REBOUND_BID)
            hit60_ask = bool(future_ask_highs and max(future_ask_highs) >= REBOUND_BID)
            minutes_to_60_bid = np.nan
            minutes_to_60_ask = np.nan
            for later in future:
                if np.isnan(minutes_to_60_bid) and later["bid_high"] >= REBOUND_BID:
                    minutes_to_60_bid = later["minute"] - dip["minute"]
                if np.isnan(minutes_to_60_ask) and later["ask_high"] >= REBOUND_BID:
                    minutes_to_60_ask = later["minute"] - dip["minute"]

            prior_ask_high = np.nanmax([x["ask_high"] for x in path[: i + 1]])
            prior_ask_low = np.nanmin([x["ask_low"] for x in path[: i + 1]])
            rows.append(
                {
                    "mode": mode,
                    "kalshi_ticker": market.kalshi_ticker,
                    "market_start": market.market_start,
                    "market_end": market.market_end,
                    "side": side,
                    "dip_minute": dip["minute"],
                    "dip_price": dip[dip_price_col],
                    "ask_close_at_dip": dip["ask_close"],
                    "bid_close_at_dip": dip["bid_close"],
                    "spread_close_at_dip": dip["spread_close"],
                    "spread_low_proxy_at_dip": dip["spread_low_proxy"],
                    "ask_low_at_dip": dip["ask_low"],
                    "ask_high_at_dip": dip["ask_high"],
                    "bid_high_at_dip": dip["bid_high"],
                    "volume_at_dip": dip["volume"],
                    "open_interest_at_dip": dip["open_interest"],
                    "prior_ask_high": prior_ask_high,
                    "prior_ask_low": prior_ask_low,
                    "drop_from_prior_high": prior_ask_high - dip[dip_price_col],
                    "max_future_bid": max(future_bid_highs) if future_bid_highs else np.nan,
                    "max_future_ask": max(future_ask_highs) if future_ask_highs else np.nan,
                    "hit60_bid": int(hit60_bid),
                    "hit60_ask": int(hit60_ask),
                    "minutes_to_60_bid": minutes_to_60_bid,
                    "minutes_to_60_ask": minutes_to_60_ask,
                    "gross_spread_exit_c": (REBOUND_BID - dip["ask_close"]) * 100,
                }
            )
    return pd.DataFrame(rows).sort_values(["market_end", "kalshi_ticker", "side"])


def summarize(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby(group_cols, observed=True)
        .agg(
            setups=("hit60_bid", "size"),
            hit60_bid=("hit60_bid", "mean"),
            hit60_ask=("hit60_ask", "mean"),
            avg_dip_price=("dip_price", "mean"),
            avg_ask_close=("ask_close_at_dip", "mean"),
            avg_spread_c=("spread_close_at_dip", lambda x: x.mean() * 100),
            median_spread_c=("spread_close_at_dip", lambda x: x.median() * 100),
            p75_spread_c=("spread_close_at_dip", lambda x: x.quantile(0.75) * 100),
            avg_gross_to_60_c=("gross_spread_exit_c", "mean"),
            median_minutes_to_60_bid=("minutes_to_60_bid", "median"),
            yes_share=("side", lambda x: (x == "YES").mean()),
        )
        .reset_index()
    )


def add_bins(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["dip_minute_bin"] = pd.cut(
        out["dip_minute"],
        bins=[0, 2, 4, 6, 8, 10],
        labels=["m1-2", "m3-4", "m5-6", "m7-8", "m9-10"],
    )
    out["spread_bin"] = pd.cut(
        out["spread_close_at_dip"],
        bins=[-1, 0.01, 0.02, 0.05, 0.10, 1],
        labels=["<=1c", "1-2c", "2-5c", "5-10c", "10c+"],
    )
    out["dip_price_bin"] = pd.cut(
        out["dip_price"],
        bins=[0.199, 0.23, 0.26, 0.30],
        labels=["20-23c", "23-26c", "26-30c"],
    )
    out["prior_high_bin"] = pd.cut(
        out["prior_ask_high"],
        bins=[0, 0.40, 0.60, 0.80, 1.01],
        labels=["<40c", "40-60c", "60-80c", "80c+"],
    )
    return out


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    close_rows = scan("close")
    touch_rows = scan("touch")
    rows = add_bins(pd.concat([close_rows, touch_rows], ignore_index=True))

    rows_path = REPORTS / "btc15_dip_to_60_rows.parquet"
    rows.to_parquet(rows_path, index=False)

    tables = {
        "overall": summarize(rows, ["mode"]),
        "by_side": summarize(rows, ["mode", "side"]),
        "by_dip_minute": summarize(rows, ["mode", "dip_minute"]),
        "by_dip_minute_bin": summarize(rows, ["mode", "dip_minute_bin"]),
        "by_spread_bin": summarize(rows, ["mode", "spread_bin"]),
        "by_dip_price_bin": summarize(rows, ["mode", "dip_price_bin"]),
        "by_prior_high_bin": summarize(rows, ["mode", "prior_high_bin"]),
    }
    summary = pd.concat(
        [table.assign(table=name) for name, table in tables.items() if not table.empty],
        ignore_index=True,
    )
    summary_path = REPORTS / "btc15_dip_to_60_summary.csv"
    summary.to_csv(summary_path, index=False)

    print(f"rows: {len(rows):,}")
    print(f"close-mode setups: {len(close_rows):,}")
    print(f"touch-mode setups: {len(touch_rows):,}")
    print(f"wrote: {rows_path.relative_to(ROOT)}")
    print(f"wrote: {summary_path.relative_to(ROOT)}")
    for name, table in tables.items():
        print(f"\n{name}")
        print(table.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
