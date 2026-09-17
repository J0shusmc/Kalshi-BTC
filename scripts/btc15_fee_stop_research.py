#!/usr/bin/env python3
"""Fresh BTC15 research with fees and stop-loss exits.

This is intentionally strategy-agnostic. It scans simple entry windows and
entry-price bands, then scores target exits, optional stop exits, fees, and
settlement. If target and stop are both touched in the same later candle, the
stop is scored first as a conservative intraminute-order assumption.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CANDLES_DIR = ROOT / "data" / "raw" / "kalshi_btc15" / "candles_by_ticker"
TRAINING = ROOT / "data" / "kalshi_btc15_t600_training.parquet"
REPORTS = ROOT / "reports"

ENTRY_WINDOWS = [(2, 3), (2, 5), (4, 6), (5, 10), (2, 10)]
ENTRY_BANDS_C = [(20, 30), (30, 40), (40, 50), (50, 60), (56, 60), (60, 70)]
TARGETS_C = [50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 99]
STOPS_C: list[int | None] = [None, 10, 15, 20, 25, 30, 35, 40, 45, 50]
MIN_TRADES = 100


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
        ask_close = price(yes_ask, "close")
        bid_low = price(yes_bid, "low")
        bid_high = price(yes_bid, "high")
        bid_close = price(yes_bid, "close")
    else:
        ask_close = 1.0 - price(yes_bid, "close")
        bid_low = 1.0 - price(yes_ask, "high")
        bid_high = 1.0 - price(yes_ask, "low")
        bid_close = 1.0 - price(yes_ask, "close")
    return {
        "ask_close_c": ask_close * 100,
        "bid_low_c": bid_low * 100,
        "bid_high_c": bid_high * 100,
        "bid_close_c": bid_close * 100,
        "spread_close_c": (ask_close - bid_close) * 100,
        "volume": num(candle.get("volume_fp", candle.get("volume"))),
        "open_interest": num(candle.get("open_interest_fp", candle.get("open_interest"))),
    }


def load_candles(ticker: str) -> list[dict]:
    path = CANDLES_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    with path.open() as fh:
        return json.load(fh).get("response", {}).get("candlesticks", [])


def build_candidates() -> pd.DataFrame:
    markets = pd.read_parquet(TRAINING)[
        ["kalshi_ticker", "market_start", "market_end", "kalshi_outcome_up"]
    ]
    rows = []
    for market in markets.itertuples(index=False):
        candles = load_candles(market.kalshi_ticker)
        if len(candles) < 4:
            continue
        for side in ("YES", "NO"):
            path = []
            for i, candle in enumerate(candles):
                point = side_prices(candle, side)
                point["minute"] = i + 1
                path.append(point)

            settle_win = (
                int(market.kalshi_outcome_up)
                if side == "YES"
                else 1 - int(market.kalshi_outcome_up)
            )
            for i, point in enumerate(path[:-1]):
                entry_c = point["ask_close_c"]
                if np.isnan(entry_c):
                    continue
                prior = np.array([p["ask_close_c"] for p in path[:i]], dtype=float)
                prior = prior[~np.isnan(prior)]
                prior_peak = float(np.max(prior)) if len(prior) else np.nan
                prior_low = float(np.min(prior)) if len(prior) else np.nan
                rows.append(
                    {
                        "kalshi_ticker": market.kalshi_ticker,
                        "market_start": market.market_start,
                        "market_end": market.market_end,
                        "side": side,
                        "minute": point["minute"],
                        "entry_c": entry_c,
                        "spread_close_c": point["spread_close_c"],
                        "volume": point["volume"],
                        "open_interest": point["open_interest"],
                        "prior_peak_c": prior_peak,
                        "prior_low_c": prior_low,
                        "pullback_from_peak_c": prior_peak - entry_c,
                        "reclaim_from_low_c": entry_c - prior_low,
                        "settle_win": settle_win,
                        "future": path[i + 1 :],
                    }
                )
    return pd.DataFrame(rows).sort_values(["market_end", "kalshi_ticker", "side", "minute"])


def score_exit(
    future: list[dict],
    entry_c: float,
    target_c: int,
    stop_c: int | None,
    settle_win: int,
    fee_c: float,
) -> tuple[str, int | None, float]:
    for point in future:
        minute = int(point["minute"])
        stop_hit = stop_c is not None and point["bid_low_c"] <= stop_c
        target_hit = point["bid_high_c"] >= target_c
        if stop_hit:
            return "stop", minute, stop_c - entry_c - fee_c
        if target_hit:
            return "target", minute, target_c - entry_c - fee_c
    if settle_win:
        return "settle_win", None, 100 - entry_c - fee_c
    return "settle_loss", None, -entry_c - fee_c


def summarize_rule(d: pd.DataFrame, group_cols: list[str]) -> dict:
    chron = d.sort_values("market_end").reset_index(drop=True)
    if len(chron) >= 4:
        chron["quartile"] = pd.qcut(
            chron.index,
            q=4,
            labels=["q1", "q2", "q3", "q4"],
            duplicates="drop",
        )
        q_ev = chron.groupby("quartile", observed=True)["pnl_c"].mean()
        min_q_ev = float(q_ev.min())
        q_ev_text = ",".join(f"{x:.2f}" for x in q_ev)
    else:
        min_q_ev = np.nan
        q_ev_text = ""

    weeks = d.copy()
    weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
    weekly_ev = weeks.groupby("week")["pnl_c"].mean()
    wins = d["pnl_c"] > 0
    losses = d["pnl_c"] < 0
    gross_profit = d.loc[wins, "pnl_c"].sum()
    gross_loss = abs(d.loc[losses, "pnl_c"].sum())
    return {
        **{col: d.iloc[0][col] for col in group_cols},
        "trades": len(d),
        "win_rate": wins.mean(),
        "avg_entry_c": d["entry_c"].mean(),
        "avg_fee_c": d["fee_c"].mean(),
        "avg_pnl_c": d["pnl_c"].mean(),
        "total_pnl_c": d["pnl_c"].sum(),
        "avg_win_c": d.loc[wins, "pnl_c"].mean() if wins.any() else np.nan,
        "avg_loss_c": d.loc[losses, "pnl_c"].mean() if losses.any() else np.nan,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else np.nan,
        "target_rate": (d["exit_type"] == "target").mean(),
        "stop_rate": (d["exit_type"] == "stop").mean(),
        "settle_loss_rate": (d["exit_type"] == "settle_loss").mean(),
        "median_exit_minute": d["exit_minute"].median(),
        "min_quartile_ev_c": min_q_ev,
        "quartile_ev_c": q_ev_text,
        "positive_weeks": int((weekly_ev > 0).sum()),
        "weeks": len(weekly_ev),
        "min_week_ev_c": float(weekly_ev.min()),
    }


def summarize_scored(
    *,
    market_order: np.ndarray,
    week_labels: np.ndarray,
    entry_c: np.ndarray,
    exit_type: list[str],
    exit_minute: list[int | None],
    pnl_c: np.ndarray,
    fee_c: float,
    params: dict[str, object],
) -> dict:
    q_values = []
    if len(market_order) >= 4:
        for chunk in np.array_split(market_order, 4):
            q_values.append(float(pnl_c[chunk].mean()))
        min_q_ev = min(q_values)
        q_ev_text = ",".join(f"{x:.2f}" for x in q_values)
    else:
        min_q_ev = np.nan
        q_ev_text = ""

    weekly_ev = pd.Series(pnl_c).groupby(week_labels).mean()
    wins = pnl_c > 0
    losses = pnl_c < 0
    gross_profit = float(pnl_c[wins].sum())
    gross_loss = abs(float(pnl_c[losses].sum()))
    exits = pd.Series(exit_type)
    exit_minutes = pd.Series(exit_minute, dtype="float64")
    return {
        **params,
        "trades": len(pnl_c),
        "win_rate": float(wins.mean()),
        "avg_entry_c": float(entry_c.mean()),
        "avg_fee_c": fee_c,
        "avg_pnl_c": float(pnl_c.mean()),
        "total_pnl_c": float(pnl_c.sum()),
        "avg_win_c": float(pnl_c[wins].mean()) if wins.any() else np.nan,
        "avg_loss_c": float(pnl_c[losses].mean()) if losses.any() else np.nan,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else np.nan,
        "target_rate": float((exits == "target").mean()),
        "stop_rate": float((exits == "stop").mean()),
        "settle_loss_rate": float((exits == "settle_loss").mean()),
        "median_exit_minute": float(exit_minutes.median()) if exit_minutes.notna().any() else np.nan,
        "min_quartile_ev_c": min_q_ev,
        "quartile_ev_c": q_ev_text,
        "positive_weeks": int((weekly_ev > 0).sum()),
        "weeks": len(weekly_ev),
        "min_week_ev_c": float(weekly_ev.min()),
    }


def evaluate(candidates: pd.DataFrame, fee_c: float) -> pd.DataFrame:
    rows = []
    for minute_min, minute_max in ENTRY_WINDOWS:
        minute_candidates = candidates[candidates["minute"].between(minute_min, minute_max)]
        for entry_min, entry_max in ENTRY_BANDS_C:
            entry_candidates = minute_candidates[
                (minute_candidates["entry_c"] >= entry_min)
                & (minute_candidates["entry_c"] < entry_max)
            ]
            if entry_candidates.empty:
                continue
            for side_filter in ("BOTH", "YES", "NO"):
                side_candidates = (
                    entry_candidates
                    if side_filter == "BOTH"
                    else entry_candidates[entry_candidates["side"] == side_filter]
                )
                if side_candidates.empty:
                    continue
                selected = (
                    side_candidates.sort_values(["market_end", "kalshi_ticker", "minute", "side"])
                    .drop_duplicates(["kalshi_ticker"], keep="first")
                    .copy()
                )
                if len(selected) < MIN_TRADES:
                    continue
                selected_base = selected[
                    [
                        "market_end",
                        "entry_c",
                        "future",
                        "settle_win",
                    ]
                ].copy()
                selected_market_end = pd.to_datetime(selected_base["market_end"], utc=True)
                market_order = np.argsort(selected_market_end.to_numpy())
                week_labels = selected_market_end.dt.strftime("%G-W%V").to_numpy()
                for target_c in TARGETS_C:
                    if target_c <= entry_min:
                        continue
                    for stop_c in STOPS_C:
                        if stop_c is not None and stop_c >= entry_min:
                            continue
                        scored = [
                            score_exit(
                                row.future,
                                float(row.entry_c),
                                target_c,
                                stop_c,
                                int(row.settle_win),
                                fee_c,
                            )
                            for row in selected_base.itertuples(index=False)
                        ]
                        rows.append(
                            summarize_scored(
                                market_order=market_order,
                                week_labels=week_labels,
                                entry_c=selected_base["entry_c"].to_numpy(dtype=float),
                                exit_type=[x[0] for x in scored],
                                exit_minute=[x[1] for x in scored],
                                pnl_c=np.array([x[2] for x in scored], dtype=float),
                                fee_c=fee_c,
                                params={
                                    "minute_min": minute_min,
                                    "minute_max": minute_max,
                                    "entry_min_c": entry_min,
                                    "entry_max_c": entry_max,
                                    "side_filter": side_filter,
                                    "target_c": target_c,
                                    "stop_c": -1 if stop_c is None else stop_c,
                                },
                            )
                        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC15 fee-aware stop-loss research.")
    parser.add_argument(
        "--fee-c",
        type=float,
        default=6.5,
        help="Round-trip fee/slippage drag in cents per contract.",
    )
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    candidates = build_candidates()
    summary = evaluate(candidates, args.fee_c)

    candidates_path = REPORTS / "btc15_fee_stop_candidates.parquet"
    summary_path = REPORTS / "btc15_fee_stop_summary.csv"
    top_path = REPORTS / "btc15_fee_stop_top.csv"

    candidates.drop(columns=["future"]).to_parquet(candidates_path, index=False)
    summary.to_csv(summary_path, index=False)

    top = summary[
        (summary["trades"] >= MIN_TRADES)
        & (summary["avg_pnl_c"] > 0)
        & (summary["min_quartile_ev_c"] > 0)
        & (summary["positive_weeks"] >= summary["weeks"] - 1)
    ].sort_values(["avg_pnl_c", "profit_factor", "trades"], ascending=[False, False, False])
    top.to_csv(top_path, index=False)

    print(f"candidates: {len(candidates):,}")
    print(f"summary rows: {len(summary):,}")
    print(f"fee_c: {args.fee_c:g}")
    print(f"wrote: {candidates_path.relative_to(ROOT)}")
    print(f"wrote: {summary_path.relative_to(ROOT)}")
    print(f"wrote: {top_path.relative_to(ROOT)}")
    print("\nTop fee-adjusted rules")
    cols = [
        "minute_min",
        "minute_max",
        "entry_min_c",
        "entry_max_c",
        "side_filter",
        "target_c",
        "stop_c",
        "trades",
        "win_rate",
        "avg_entry_c",
        "avg_pnl_c",
        "avg_win_c",
        "avg_loss_c",
        "profit_factor",
        "target_rate",
        "stop_rate",
        "settle_loss_rate",
        "min_quartile_ev_c",
        "positive_weeks",
        "weeks",
    ]
    print(top[cols].head(30).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
