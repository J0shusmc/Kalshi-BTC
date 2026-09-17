#!/usr/bin/env python3
"""Buy post-minute-five dips that oppose a fixed directional model signal.

The signal is frozen after minute five. A limit order for the predicted side is
then eligible from minute six through a configured cutoff. Targets are scored
only on candles after the fill candle to avoid optimistic intraminute ordering.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
PREDICTIONS = REPORTS / "btc15_first5_outcome_predictions.parquet"
CANDLES_DIR = ROOT / "data" / "raw" / "kalshi_btc15" / "candles_by_ticker"

ENTRY_LIMITS_C = (20, 25, 30, 35, 40)
ENTRY_PENETRATIONS_C = (0, 1, 2)
ENTRY_END_MINUTES = (8, 10, 12, 14)
TARGETS_C = (40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90)
TARGET_PENETRATIONS_C = (0, 1, 2)
MODEL_COLUMNS = (
    "p_btc_signal",
    "p_kalshi_signal",
    "p_dual_stack",
    "p_market_blend",
    "p_market",
)


def number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def node_price(candle: dict, node: str, field: str) -> float:
    payload = candle.get(node, {}) or {}
    return number(payload.get(f"{field}_dollars", payload.get(field)))


def side_point(candle: dict, side: str) -> dict[str, float]:
    yes_ask_low = node_price(candle, "yes_ask", "low")
    yes_bid_high = node_price(candle, "yes_bid", "high")
    if side == "YES":
        return {"ask_low_c": yes_ask_low * 100, "bid_high_c": yes_bid_high * 100}
    return {
        "ask_low_c": (1 - yes_bid_high) * 100,
        "bid_high_c": (1 - yes_ask_low) * 100,
    }


def market_paths(ticker: str) -> dict[str, list[dict[str, float]]]:
    path = CANDLES_DIR / f"{ticker}.json"
    if not path.exists():
        return {}
    candles = json.loads(path.read_text()).get("response", {}).get("candlesticks", [])
    candles = sorted(candles, key=lambda row: int(row.get("end_period_ts") or 0))
    return {
        side: [
            {"minute": minute, **side_point(candle, side)}
            for minute, candle in enumerate(candles, start=1)
        ]
        for side in ("YES", "NO")
    }


def build_fill_table(predictions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    outcomes = predictions.set_index("kalshi_ticker")["kalshi_outcome_up"].to_dict()
    market_ends = predictions.set_index("kalshi_ticker")["market_end"].to_dict()
    for ticker in predictions["kalshi_ticker"].drop_duplicates():
        paths = market_paths(ticker)
        if not paths:
            continue
        outcome_up = int(outcomes[ticker])
        for side, points in paths.items():
            settle_win = outcome_up if side == "YES" else 1 - outcome_up
            for entry_c in ENTRY_LIMITS_C:
                for entry_penetration_c in ENTRY_PENETRATIONS_C:
                    for end_minute in ENTRY_END_MINUTES:
                        eligible = [
                            point
                            for point in points
                            if 6 <= int(point["minute"]) <= end_minute
                            and point["ask_low_c"] <= entry_c - entry_penetration_c
                        ]
                        if not eligible:
                            continue
                        fill = eligible[0]
                        future_bids = [
                            point["bid_high_c"]
                            for point in points
                            if int(point["minute"]) > int(fill["minute"])
                            and not np.isnan(point["bid_high_c"])
                        ]
                        rows.append(
                            {
                                "kalshi_ticker": ticker,
                                "market_end": market_ends[ticker],
                                "side": side,
                                "entry_c": entry_c,
                                "entry_penetration_c": entry_penetration_c,
                                "entry_end_minute": end_minute,
                                "fill_minute": int(fill["minute"]),
                                "fill_ask_low_c": fill["ask_low_c"],
                                "max_future_bid_c": max(future_bids) if future_bids else np.nan,
                                "settle_win": settle_win,
                            }
                        )
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame, pnl_c: np.ndarray) -> dict[str, float]:
    wins = pnl_c > 0
    losses = pnl_c < 0
    gross_profit = pnl_c[wins].sum()
    gross_loss = -pnl_c[losses].sum()
    return {
        "trades": len(trades),
        "avg_pnl_c": pnl_c.mean() if len(pnl_c) else np.nan,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else np.nan,
        "win_rate": wins.mean() if len(pnl_c) else np.nan,
        "avg_win_c": pnl_c[wins].mean() if wins.any() else np.nan,
        "avg_loss_c": pnl_c[losses].mean() if losses.any() else np.nan,
        "median_fill_minute": trades["fill_minute"].median() if len(trades) else np.nan,
    }


def select_directional(
    candidates: pd.DataFrame, model: str, confidence: float
) -> pd.DataFrame:
    probability = candidates[model]
    return candidates[
        ((candidates["side"] == "YES") & (probability >= 0.5 + confidence))
        | ((candidates["side"] == "NO") & (probability <= 0.5 - confidence))
    ]


def score_selected(
    selected: pd.DataFrame,
    entry_c: int,
    target_c: int,
    target_penetration_c: int,
    fee_c: float,
) -> dict[str, float]:
    target_hit = selected["max_future_bid_c"] >= target_c + target_penetration_c
    settlement_pnl = np.where(
        selected["settle_win"].to_numpy(dtype=bool),
        100 - entry_c - fee_c,
        -entry_c - fee_c,
    )
    pnl_c = np.where(target_hit, target_c - entry_c - fee_c, settlement_pnl)
    return summarize(selected, pnl_c)


def search(predictions: pd.DataFrame, fills: pd.DataFrame, fee_c: float) -> pd.DataFrame:
    candidates = fills.merge(
        predictions[["kalshi_ticker", "fold", *MODEL_COLUMNS]],
        on="kalshi_ticker",
        how="inner",
        validate="many_to_one",
    )
    discovery = candidates[candidates["fold"].between(0, 2)]
    validation = candidates[candidates["fold"] == 3]
    discovery_groups = {
        key: group
        for key, group in discovery.groupby(
            ["entry_c", "entry_penetration_c", "entry_end_minute"]
        )
    }
    validation_groups = {
        key: group
        for key, group in validation.groupby(
            ["entry_c", "entry_penetration_c", "entry_end_minute"]
        )
    }
    rows = []
    for model in MODEL_COLUMNS:
        for confidence in np.arange(0.025, 0.301, 0.025):
            for entry_c in ENTRY_LIMITS_C:
                for entry_penetration_c in ENTRY_PENETRATIONS_C:
                    for entry_end_minute in ENTRY_END_MINUTES:
                        key = (entry_c, entry_penetration_c, entry_end_minute)
                        train_candidates = discovery_groups.get(key, discovery.iloc[:0])
                        test_candidates = validation_groups.get(key, validation.iloc[:0])
                        train_selected = select_directional(train_candidates, model, confidence)
                        if len(train_selected) < 50:
                            continue
                        test_selected = select_directional(test_candidates, model, confidence)
                        for target_c in TARGETS_C:
                            if target_c <= entry_c:
                                continue
                            for target_penetration_c in TARGET_PENETRATIONS_C:
                                train = score_selected(
                                    train_selected,
                                    entry_c,
                                    target_c,
                                    target_penetration_c,
                                    fee_c,
                                )
                                test = score_selected(
                                    test_selected,
                                    entry_c,
                                    target_c,
                                    target_penetration_c,
                                    fee_c,
                                )
                                rows.append(
                                    {
                                        "model": model,
                                        "confidence": confidence,
                                        "entry_c": entry_c,
                                        "entry_penetration_c": entry_penetration_c,
                                        "entry_end_minute": entry_end_minute,
                                        "target_c": target_c,
                                        "target_penetration_c": target_penetration_c,
                                        **{
                                            f"discovery_{metric}": value
                                            for metric, value in train.items()
                                        },
                                        **{
                                            f"validation_{metric}": value
                                            for metric, value in test.items()
                                        },
                                    }
                                )
    return pd.DataFrame(rows).sort_values(
        ["discovery_profit_factor", "discovery_trades"], ascending=False
    )


def write_report(results: pd.DataFrame, fee_c: float) -> None:
    repeated = results[
        (results["discovery_profit_factor"] > 1)
        & (results["validation_profit_factor"] > 1)
        & (results["validation_trades"] >= 20)
    ].copy()
    if not repeated.empty:
        repeated["joint_profit_factor"] = repeated[
            ["discovery_profit_factor", "validation_profit_factor"]
        ].min(axis=1)
        repeated = repeated.sort_values(
            ["joint_profit_factor", "validation_trades", "target_penetration_c"],
            ascending=False,
        ).drop_duplicates(
            [
                "model",
                "confidence",
                "entry_c",
                "entry_penetration_c",
                "entry_end_minute",
                "target_c",
            ]
        )
    pf5 = repeated[
        (repeated["discovery_profit_factor"] >= 5)
        & (repeated["validation_profit_factor"] >= 5)
    ]
    display_columns = [
        "model",
        "confidence",
        "entry_c",
        "entry_penetration_c",
        "entry_end_minute",
        "target_c",
        "target_penetration_c",
        "discovery_trades",
        "discovery_avg_pnl_c",
        "discovery_profit_factor",
        "validation_trades",
        "validation_avg_pnl_c",
        "validation_profit_factor",
    ]
    text = [
        "# BTC15 Directional Dip Research",
        "",
        "Freeze an UP/DOWN model after minute five, buy only a later 20-40c dip in that direction, and scalp a nearer target.",
        "",
        f"- Fee/slippage allowance: `{fee_c:.1f}c`",
        "- Entries begin: minute `6`",
        "- Same-candle target touches: excluded",
        "- Discovery folds: `0-2`",
        "- Untouched validation fold: `3`",
        f"- Repeated profitable parameter sets: `{len(repeated)}`",
        f"- Repeated PF >= 5 parameter sets: `{len(pf5)}`",
        "",
        "## Best Repeated Rules",
        "",
        "```text",
        repeated[display_columns].head(30).round(4).to_string(index=False)
        if not repeated.empty
        else "No rule was profitable in both discovery and validation.",
        "```",
    ]
    (REPORTS / "btc15_directional_dip_research.md").write_text("\n".join(text) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fee-c", type=float, default=6.5)
    args = parser.parse_args()
    predictions = pd.read_parquet(PREDICTIONS)
    predictions["market_end"] = pd.to_datetime(predictions["market_end"], utc=True)
    fills = build_fill_table(predictions)
    fills.to_parquet(REPORTS / "btc15_directional_dip_fills.parquet", index=False)
    results = search(predictions, fills, args.fee_c)
    results.to_csv(REPORTS / "btc15_directional_dip_search.csv", index=False)
    write_report(results, args.fee_c)
    print(results.head(30).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
