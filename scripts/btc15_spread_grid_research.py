#!/usr/bin/env python3
"""Grid-test BTC15 spread-bot entries and exits.

Assumption being tested:
- Rest a buy limit on YES and/or NO during minutes 1-10.
- If filled, stop buying after minute 10 and only manage exits.
- Winner: a later candle's bid high reaches the target.
- Loser: target never appears; score as full entry loss.

This deliberately does not use final settlement as the target.
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

ENTRY_LIMITS_C = range(20, 36)
EXIT_TARGETS_C = range(30, 71)
MAX_ENTRY_MINUTE = 10
MIN_EDGE_C = 0.0
MIN_HIT_RATE = 0.50
MIN_TRADES = 250


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
        ask_low = price(yes_ask, "low")
        ask_close = price(yes_ask, "close")
        bid_high = price(yes_bid, "high")
        bid_close = price(yes_bid, "close")
    else:
        ask_low = 1.0 - price(yes_bid, "high")
        ask_close = 1.0 - price(yes_bid, "close")
        bid_high = 1.0 - price(yes_ask, "low")
        bid_close = 1.0 - price(yes_ask, "close")
    return {
        "ask_low": ask_low,
        "ask_close": ask_close,
        "bid_high": bid_high,
        "bid_close": bid_close,
        "spread_close": ask_close - bid_close,
        "volume": num(candle.get("volume_fp", candle.get("volume"))),
        "open_interest": num(candle.get("open_interest_fp", candle.get("open_interest"))),
    }


def load_candles(ticker: str) -> list[dict]:
    path = CANDLES_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    with path.open() as fh:
        return json.load(fh).get("response", {}).get("candlesticks", [])


def build_fills() -> pd.DataFrame:
    markets = pd.read_parquet(TRAINING)[["kalshi_ticker", "market_start", "market_end"]]
    rows = []
    for market in markets.itertuples(index=False):
        candles = load_candles(market.kalshi_ticker)
        if len(candles) <= MAX_ENTRY_MINUTE:
            continue

        for side in ("YES", "NO"):
            path = []
            for i, candle in enumerate(candles):
                vals = side_prices(candle, side)
                vals["minute"] = i + 1
                path.append(vals)

            for entry_c in ENTRY_LIMITS_C:
                entry = entry_c / 100
                fill_indices = [
                    i
                    for i, point in enumerate(path[:MAX_ENTRY_MINUTE])
                    if not np.isnan(point["ask_low"]) and point["ask_low"] <= entry
                ]
                if not fill_indices:
                    continue

                i = fill_indices[0]
                fill = path[i]
                future = path[i + 1 :]
                max_future_bid = np.nanmax(
                    [point["bid_high"] for point in future if not np.isnan(point["bid_high"])]
                    or [np.nan]
                )
                rows.append(
                    {
                        "kalshi_ticker": market.kalshi_ticker,
                        "market_start": market.market_start,
                        "market_end": market.market_end,
                        "side": side,
                        "entry_c": entry_c,
                        "fill_minute": fill["minute"],
                        "ask_low_at_fill": fill["ask_low"],
                        "ask_close_at_fill": fill["ask_close"],
                        "bid_close_at_fill": fill["bid_close"],
                        "spread_close_at_fill_c": fill["spread_close"] * 100,
                        "volume_at_fill": fill["volume"],
                        "open_interest_at_fill": fill["open_interest"],
                        "max_future_bid_c": max_future_bid * 100,
                    }
                )
    return pd.DataFrame(rows).sort_values(["market_end", "kalshi_ticker", "side", "entry_c"])


def evaluate_grid(fills: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    trade_rows = []
    for side_filter in ["BOTH", "YES", "NO"]:
        side_fills = fills if side_filter == "BOTH" else fills[fills["side"] == side_filter]
        for entry_c in ENTRY_LIMITS_C:
            entry_fills = side_fills[side_fills["entry_c"] == entry_c].copy()
            if entry_fills.empty:
                continue
            for target_c in EXIT_TARGETS_C:
                if target_c <= entry_c:
                    continue
                d = entry_fills.copy()
                d["target_c"] = target_c
                d["hit_target"] = d["max_future_bid_c"] >= target_c
                d["pnl_c"] = np.where(d["hit_target"], target_c - entry_c, -entry_c)
                d["breakeven_hit_rate"] = entry_c / target_c
                d["side_filter"] = side_filter

                chron = d.sort_values("market_end").reset_index(drop=True)
                if len(chron) >= 4:
                    chron["quartile"] = pd.qcut(
                        chron.index,
                        q=4,
                        labels=["q1", "q2", "q3", "q4"],
                        duplicates="drop",
                    )
                    q_ev = chron.groupby("quartile", observed=True)["pnl_c"].mean()
                    q_hit = chron.groupby("quartile", observed=True)["hit_target"].mean()
                    min_q_ev = float(q_ev.min())
                    q_ev_text = ",".join(f"{x:.2f}" for x in q_ev)
                    q_hit_text = ",".join(f"{x:.3f}" for x in q_hit)
                else:
                    min_q_ev = np.nan
                    q_ev_text = ""
                    q_hit_text = ""

                weeks = d.copy()
                weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
                weekly_ev = weeks.groupby("week")["pnl_c"].mean()
                rows.append(
                    {
                        "side_filter": side_filter,
                        "entry_c": entry_c,
                        "target_c": target_c,
                        "gross_win_c": target_c - entry_c,
                        "loss_c": entry_c,
                        "breakeven_hit_rate": entry_c / target_c,
                        "trades": len(d),
                        "hit_rate": d["hit_target"].mean(),
                        "edge_vs_breakeven": d["hit_target"].mean() - entry_c / target_c,
                        "avg_pnl_c": d["pnl_c"].mean(),
                        "total_pnl_c": d["pnl_c"].sum(),
                        "median_fill_minute": d["fill_minute"].median(),
                        "avg_spread_at_fill_c": d["spread_close_at_fill_c"].mean(),
                        "median_spread_at_fill_c": d["spread_close_at_fill_c"].median(),
                        "min_quartile_ev_c": min_q_ev,
                        "quartile_ev_c": q_ev_text,
                        "quartile_hit_rate": q_hit_text,
                        "positive_weeks": int((weekly_ev > MIN_EDGE_C).sum()),
                        "weeks": len(weekly_ev),
                        "min_week_ev_c": float(weekly_ev.min()),
                    }
                )
                if side_filter in ("YES", "NO"):
                    trade_rows.append(
                        d[
                            [
                                "kalshi_ticker",
                                "market_end",
                                "side",
                                "side_filter",
                                "entry_c",
                                "target_c",
                                "fill_minute",
                                "hit_target",
                                "pnl_c",
                                "max_future_bid_c",
                                "spread_close_at_fill_c",
                            ]
                        ]
                    )
    return pd.DataFrame(rows), pd.concat(trade_rows, ignore_index=True)


def format_top(df: pd.DataFrame) -> pd.DataFrame:
    keep = df[
        (df["trades"] >= MIN_TRADES)
        & (df["hit_rate"] > MIN_HIT_RATE)
        & (df["avg_pnl_c"] > MIN_EDGE_C)
        & (df["min_quartile_ev_c"] > MIN_EDGE_C)
    ].copy()
    return keep.sort_values(["avg_pnl_c", "trades"], ascending=[False, False]).head(30)


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    fills = build_fills()
    fills_path = REPORTS / "btc15_spread_grid_fills.parquet"
    fills.to_parquet(fills_path, index=False)

    grid, trades = evaluate_grid(fills)
    grid_path = REPORTS / "btc15_spread_grid_summary.csv"
    trades_path = REPORTS / "btc15_spread_grid_trades.parquet"
    grid.to_csv(grid_path, index=False)
    trades.to_parquet(trades_path, index=False)

    top = format_top(grid)
    top_path = REPORTS / "btc15_spread_grid_top.csv"
    top.to_csv(top_path, index=False)

    print(f"fills: {len(fills):,}")
    print(f"grid rows: {len(grid):,}")
    print(f"wrote: {fills_path.relative_to(ROOT)}")
    print(f"wrote: {grid_path.relative_to(ROOT)}")
    print(f"wrote: {top_path.relative_to(ROOT)}")
    print(f"wrote: {trades_path.relative_to(ROOT)}")
    print("\nTop robust spreads: trades>=250, hit>50%, EV>0, all quartiles EV>0")
    cols = [
        "side_filter",
        "entry_c",
        "target_c",
        "gross_win_c",
        "loss_c",
        "breakeven_hit_rate",
        "trades",
        "hit_rate",
        "edge_vs_breakeven",
        "avg_pnl_c",
        "median_fill_minute",
        "median_spread_at_fill_c",
        "min_quartile_ev_c",
        "positive_weeks",
        "weeks",
        "min_week_ev_c",
        "quartile_ev_c",
    ]
    print(top[cols].round(4).to_string(index=False))


if __name__ == "__main__":
    main()
