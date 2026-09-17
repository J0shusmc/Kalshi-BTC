#!/usr/bin/env python3
"""Research delayed BTC15 entries that become cheaper after the open.

This scans per-minute Kalshi candles for YES/NO asks that close in a target
entry band, then measures whether a later bid touches 99c. Same-candle exits
are intentionally ignored so the result is less dependent on intraminute order.
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

ENTRY_MIN = 0.50
ENTRY_MAX = 0.60
EXIT_BID = 0.99


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
        return {
            "ask_open": price(yes_ask, "open"),
            "ask_close": price(yes_ask, "close"),
            "ask_low": price(yes_ask, "low"),
            "ask_high": price(yes_ask, "high"),
            "bid_high": price(yes_bid, "high"),
        }
    return {
        "ask_open": 1.0 - price(yes_bid, "open"),
        "ask_close": 1.0 - price(yes_bid, "close"),
        "ask_low": 1.0 - price(yes_bid, "high"),
        "ask_high": 1.0 - price(yes_bid, "low"),
        "bid_high": 1.0 - price(yes_ask, "low"),
    }


def load_candles(ticker: str) -> list[dict]:
    path = CANDLES_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    with path.open() as fh:
        return json.load(fh).get("response", {}).get("candlesticks", [])


def scan_entries() -> pd.DataFrame:
    training = pd.read_parquet(TRAINING)[
        ["kalshi_ticker", "market_start", "market_end", "kalshi_outcome_up"]
    ]
    rows = []

    for market in training.itertuples(index=False):
        candles = load_candles(market.kalshi_ticker)
        if len(candles) < 10:
            continue

        for side in ("YES", "NO"):
            side_path = []
            for i, candle in enumerate(candles):
                vals = side_prices(candle, side)
                vals["minute"] = i + 1
                vals["end_period_ts"] = candle.get("end_period_ts")
                side_path.append(vals)

            asks = [x["ask_close"] for x in side_path]
            entry_indices = [
                i
                for i, x in enumerate(side_path)
                if ENTRY_MIN <= x["ask_close"] < ENTRY_MAX
                and i >= 1
                and not np.isnan(x["ask_close"])
            ]
            if not entry_indices:
                continue

            open_ask = side_path[0]["ask_close"]
            settle_win = (
                int(market.kalshi_outcome_up)
                if side == "YES"
                else 1 - int(market.kalshi_outcome_up)
            )

            for i in entry_indices:
                entry = side_path[i]
                prior_closes = np.array(asks[:i], dtype=float)
                prior_closes = prior_closes[~np.isnan(prior_closes)]
                if len(prior_closes) == 0:
                    continue
                prior_peak = float(np.max(prior_closes))
                prior_min = float(np.min(prior_closes))
                prior_last = float(prior_closes[-1])
                future_bid_highs = [
                    x["bid_high"]
                    for x in side_path[i + 1 :]
                    if not np.isnan(x["bid_high"])
                ]
                hit99 = bool(future_bid_highs and max(future_bid_highs) >= EXIT_BID)
                minutes99 = np.nan
                if hit99:
                    for later in side_path[i + 1 :]:
                        if later["bid_high"] >= EXIT_BID:
                            minutes99 = later["minute"] - entry["minute"]
                            break

                cost = entry["ask_close"]
                pnl_hold = 1.0 - cost if settle_win else -cost
                pnl_99 = EXIT_BID - cost if hit99 else pnl_hold
                rows.append(
                    {
                        "kalshi_ticker": market.kalshi_ticker,
                        "market_start": market.market_start,
                        "market_end": market.market_end,
                        "side": side,
                        "entry_minute": entry["minute"],
                        "cost": cost,
                        "open_ask_close": open_ask,
                        "prior_last_ask": prior_last,
                        "prior_peak_ask": prior_peak,
                        "prior_min_ask": prior_min,
                        "drop_from_prior": prior_last - cost,
                        "drop_from_peak": prior_peak - cost,
                        "rebound_from_prior_min": cost - prior_min,
                        "settle_win": settle_win,
                        "hit99": int(hit99),
                        "minutes99": minutes99,
                        "pnl_hold": pnl_hold,
                        "pnl_99": pnl_99,
                    }
                )

    return pd.DataFrame(rows).sort_values(["market_end", "kalshi_ticker", "side", "entry_minute"])


def one_per_market(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    ranked = candidates.copy()
    ranked["drop_rank"] = ranked["drop_from_peak"]
    ranked = ranked.sort_values(
        ["market_end", "kalshi_ticker", "drop_rank", "entry_minute"],
        ascending=[True, True, False, True],
    )
    return ranked.groupby("kalshi_ticker", as_index=False).head(1)


def summarize(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby(group_cols, observed=True)
        .agg(
            trades=("hit99", "size"),
            avg_cost=("cost", "mean"),
            hit99=("hit99", "mean"),
            settle_win=("settle_win", "mean"),
            avg_pnl_99_c=("pnl_99", lambda x: x.mean() * 100),
            avg_pnl_hold_c=("pnl_hold", lambda x: x.mean() * 100),
            median_minutes99=("minutes99", "median"),
            yes_share=("side", lambda x: (x == "YES").mean()),
        )
        .reset_index()
    )


def add_bins(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["entry_time_bin"] = pd.cut(
        out["entry_minute"],
        bins=[1, 3, 5, 8, 11, 15],
        labels=["m2-3", "m4-5", "m6-8", "m9-11", "m12-15"],
        include_lowest=True,
    )
    out["drop_peak_bin"] = pd.cut(
        out["drop_from_peak"],
        bins=[-1, 0, 0.05, 0.10, 0.20, 1],
        labels=["none", "0-5c", "5-10c", "10-20c", "20c+"],
    )
    out["prior_peak_bin"] = pd.cut(
        out["prior_peak_ask"],
        bins=[0, 0.60, 0.70, 0.80, 0.90, 1.01],
        labels=["<60c", "60-70c", "70-80c", "80-90c", "90c+"],
    )
    return out


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    entries = add_bins(scan_entries())
    entries_path = REPORTS / "btc15_wait_for_cheaper_entries.parquet"
    entries.to_parquet(entries_path, index=False)

    selected = one_per_market(entries)
    selected_path = REPORTS / "btc15_wait_for_cheaper_selected.parquet"
    selected.to_parquet(selected_path, index=False)

    tables = {
        "all_entries_by_side": summarize(entries, ["side"]),
        "selected_by_side": summarize(selected, ["side"]),
        "selected_by_entry_time": summarize(selected, ["entry_time_bin"]),
        "selected_by_drop_from_peak": summarize(selected, ["drop_peak_bin"]),
        "selected_by_prior_peak": summarize(selected, ["prior_peak_bin"]),
        "selected_by_entry_time_and_drop": summarize(selected, ["entry_time_bin", "drop_peak_bin"]),
    }
    summary_path = REPORTS / "btc15_wait_for_cheaper_summary.csv"
    pd.concat(
        [table.assign(table=name) for name, table in tables.items() if not table.empty],
        ignore_index=True,
    ).to_csv(summary_path, index=False)

    print(f"entries: {len(entries):,}")
    print(f"selected one per market: {len(selected):,}")
    print(f"wrote: {entries_path.relative_to(ROOT)}")
    print(f"wrote: {selected_path.relative_to(ROOT)}")
    print(f"wrote: {summary_path.relative_to(ROOT)}")
    for name, table in tables.items():
        print(f"\n{name}")
        print(table.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
