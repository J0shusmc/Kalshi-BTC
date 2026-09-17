#!/usr/bin/env python3
"""Research Kalshi BTC15 side-price EMA21 mean reversion.

This tests the chart shape Joshua described:
- Treat each YES/NO side as its own price series.
- Compute EMA21 on the Kalshi side ask close.
- A setup is a side trading below its EMA after moving away from a recent EMA
  touch.
- The side must have dropped enough from recent highs to create a 20-25c entry.
- Buy window is the signal bar and the following bar only.
- Score the trade as an 80c bid target, otherwise full entry loss.

This is isolated research and is not imported by the live BTC bot.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
MINUTES = REPORTS / "btc15_minute_candidates.parquet"
TRAINING = ROOT / "data" / "kalshi_btc15_t600_training.parquet"

EMA_SPAN = 21
ENTRY_LIMITS_C = range(20, 26)
TARGET_C = 80
ENTRY_WINDOW_BARS = 2
MAX_ENTRY_MINUTE = 10

MIN_DIST_FROM_EMA_C = [8, 10, 12, 15, 20, 25, 30]
BARS_SINCE_TOUCH = [1, 2, 3, 4, 5, 6, 8]
MIN_DROP_FROM_RECENT_HIGH_C = [20, 25, 30, 35, 40, 50]
RECENT_HIGH_WINDOWS = [3, 5, 8, 13]
TOUCH_TOLERANCE_C = 2
MIN_TRADES = 20


def load_rows() -> pd.DataFrame:
    rows = pd.read_parquet(MINUTES).copy()
    rows["kalshi_ticker"] = rows["kalshi_ticker"].astype(str)
    rows["side"] = rows["side"].astype(str)
    rows["market_end"] = pd.to_datetime(rows["market_end"], utc=True)
    rows["market_start"] = rows["market_end"] - pd.Timedelta(minutes=15)
    rows["minute_end"] = rows["market_start"] + pd.to_timedelta(rows["minute"], unit="m")
    outcomes = pd.read_parquet(TRAINING)[["kalshi_ticker", "kalshi_outcome_up"]]
    rows = rows.merge(outcomes, on="kalshi_ticker", how="left")
    rows["settle_win"] = np.where(
        rows["side"].eq("YES"),
        rows["kalshi_outcome_up"],
        1 - rows["kalshi_outcome_up"],
    ).astype(int)
    rows = rows.sort_values(["side", "minute_end", "kalshi_ticker"]).reset_index(drop=True)
    return rows


def bars_since_last_touch(touched: pd.Series) -> pd.Series:
    counts = []
    bars = 0
    seen_touch = False
    for value in touched:
        if bool(value):
            bars = 0
            seen_touch = True
        else:
            bars = bars + 1 if seen_touch else np.nan
        counts.append(bars)
    return pd.Series(counts, index=touched.index, dtype="float64")


def add_ema_features(rows: pd.DataFrame) -> pd.DataFrame:
    out = []
    for side, group in rows.groupby("side", sort=False):
        g = group.sort_values(["minute_end", "kalshi_ticker"]).copy()
        g["ask_ema21"] = g["ask_close"].ewm(span=EMA_SPAN, adjust=False).mean()
        g["dist_below_ema_c"] = g["ask_ema21"] - g["ask_close"]
        g["touch_ema21"] = (
            (g["ask_low"] <= g["ask_ema21"] + TOUCH_TOLERANCE_C)
            & (g["ask_high"] >= g["ask_ema21"] - TOUCH_TOLERANCE_C)
        )
        g["bars_since_ema_touch"] = bars_since_last_touch(g["touch_ema21"])
        for window in RECENT_HIGH_WINDOWS:
            recent_high = g["ask_high"].rolling(window, min_periods=1).max().shift(1)
            g[f"drop_from_high_{window}_c"] = recent_high - g["ask_low"]
        out.append(g)
    return pd.concat(out, ignore_index=True).sort_values(["market_end", "kalshi_ticker", "side", "minute"])


def build_entries(rows: pd.DataFrame) -> pd.DataFrame:
    rows = rows[rows["minute"] <= MAX_ENTRY_MINUTE].copy()
    base_cols = [
        "kalshi_ticker",
        "market_end",
        "market_start",
        "minute_end",
        "side",
        "minute",
        "ask_open",
        "ask_close",
        "ask_low",
        "ask_high",
        "bid_high",
        "future_max_bid",
        "settle_win",
        "ask_ema21",
        "dist_below_ema_c",
        "bars_since_ema_touch",
        *[f"drop_from_high_{window}_c" for window in RECENT_HIGH_WINDOWS],
    ]
    current = rows[base_cols].copy()
    current["signal_minute"] = current["minute"]
    current["signal_ask_ema21"] = current["ask_ema21"]
    current["signal_dist_below_ema_c"] = current["dist_below_ema_c"]
    current["signal_bars_since_touch"] = current["bars_since_ema_touch"]
    current["entry_minutes_after_signal"] = 0

    previous = rows[base_cols].copy()
    grouped = previous.groupby(["kalshi_ticker", "side"], observed=True, sort=False)
    previous["signal_minute"] = grouped["minute"].shift(1)
    previous["signal_ask_ema21"] = grouped["ask_ema21"].shift(1)
    previous["signal_dist_below_ema_c"] = grouped["dist_below_ema_c"].shift(1)
    previous["signal_bars_since_touch"] = grouped["bars_since_ema_touch"].shift(1)
    previous["entry_minutes_after_signal"] = previous["minute"] - previous["signal_minute"]
    previous = previous[previous["entry_minutes_after_signal"].eq(1)].copy()

    candidates = pd.concat([current, previous], ignore_index=True)
    candidates = candidates[
        candidates["signal_dist_below_ema_c"].notna()
        & candidates["signal_bars_since_touch"].notna()
    ].copy()
    candidates = candidates.sort_values(["market_end", "kalshi_ticker", "side", "minute"])

    entries = []
    for min_dist in MIN_DIST_FROM_EMA_C:
        for min_bars in BARS_SINCE_TOUCH:
            signal_filtered = candidates[
                (candidates["signal_dist_below_ema_c"] >= min_dist)
                & (candidates["signal_bars_since_touch"] >= min_bars)
            ]
            if signal_filtered.empty:
                continue

            for window in RECENT_HIGH_WINDOWS:
                drop_col = f"drop_from_high_{window}_c"
                for min_drop in MIN_DROP_FROM_RECENT_HIGH_C:
                    dropped = signal_filtered[signal_filtered[drop_col] >= min_drop]
                    if dropped.empty:
                        continue
                    for entry_c in ENTRY_LIMITS_C:
                        fills = dropped[
                            (dropped["ask_low"] <= entry_c)
                            & (dropped["ask_close"] <= entry_c + 10)
                        ].copy()
                        if fills.empty:
                            continue
                        first = fills[~fills.duplicated(["kalshi_ticker", "side"], keep="first")].copy()
                        first["entry_c"] = entry_c
                        first["rule"] = (
                            f"dist{min_dist}_bars{min_bars}_drop{min_drop}_"
                            f"high{window}_entry{entry_c}"
                        )
                        first["min_dist_c"] = min_dist
                        first["min_bars_since_touch"] = min_bars
                        first["min_drop_c"] = min_drop
                        first["recent_high_window"] = window
                        first["drop_from_recent_high_c"] = first[drop_col]
                        entries.append(first)
    if not entries:
        return pd.DataFrame()
    out = pd.concat(entries, ignore_index=True)
    out["entry_minutes_after_signal"] = out["minute"] - out["signal_minute"]
    out["hit80"] = out["future_max_bid"] >= TARGET_C
    out["pnl_c"] = np.where(out["hit80"], TARGET_C - out["entry_c"], -out["entry_c"])
    return out.sort_values(["market_end", "kalshi_ticker", "side", "rule"])


def build_diagnostic_entries(rows: pd.DataFrame) -> pd.DataFrame:
    rows = rows[rows["minute"] <= MAX_ENTRY_MINUTE].sort_values(
        ["market_end", "kalshi_ticker", "side", "minute"]
    )
    current = rows.copy()
    current["signal_minute"] = current["minute"]
    current["signal_dist_below_ema_c"] = current["dist_below_ema_c"]
    current["signal_bars_since_touch"] = current["bars_since_ema_touch"]
    current["entry_minutes_after_signal"] = 0

    previous = rows.copy()
    grouped = previous.groupby(["kalshi_ticker", "side"], observed=True, sort=False)
    previous["signal_minute"] = grouped["minute"].shift(1)
    previous["signal_dist_below_ema_c"] = grouped["dist_below_ema_c"].shift(1)
    previous["signal_bars_since_touch"] = grouped["bars_since_ema_touch"].shift(1)
    previous["entry_minutes_after_signal"] = previous["minute"] - previous["signal_minute"]
    previous = previous[previous["entry_minutes_after_signal"].eq(1)]

    out = pd.concat([current, previous], ignore_index=True).sort_values(
        ["market_end", "kalshi_ticker", "side", "minute"]
    )
    out["drop5_c"] = out["drop_from_high_5_c"]
    out = out[
        out["signal_dist_below_ema_c"].notna()
        & out["signal_bars_since_touch"].notna()
        & (out["signal_dist_below_ema_c"] > 0)
        & (out["drop5_c"] >= 20)
        & (out["ask_low"] <= 25)
        & (out["ask_close"] <= 35)
    ].copy()
    out = out[~out.duplicated(["kalshi_ticker", "side"], keep="first")].copy()
    out["entry_c"] = np.minimum(25, np.maximum(20, np.ceil(out["ask_low"]))).astype(int)
    out["hit80"] = out["future_max_bid"] >= TARGET_C
    out["pnl_c"] = np.where(out["hit80"], TARGET_C - out["entry_c"], -out["entry_c"])
    out["dist_bin"] = pd.cut(
        out["signal_dist_below_ema_c"],
        [0, 5, 10, 15, 20, 25, 30, 40, 60, 100],
        labels=["0-5", "5-10", "10-15", "15-20", "20-25", "25-30", "30-40", "40-60", "60+"],
    )
    out["bars_bin"] = pd.cut(
        out["signal_bars_since_touch"],
        [0, 1, 2, 3, 5, 8, 13, 99],
        labels=["1", "2", "3", "4-5", "6-8", "9-13", "14+"],
    )
    return out


def summarize_diagnostic(entries: pd.DataFrame, group_cols: list[str], min_trades: int = 15) -> pd.DataFrame:
    rows = []
    for keys, g in entries.groupby(group_cols, observed=True):
        if len(g) < min_trades:
            continue
        if not isinstance(keys, tuple):
            keys = (keys,)
        chron = g.sort_values("market_end").reset_index(drop=True)
        chron["quartile"] = np.minimum((np.arange(len(chron)) * 4) // len(chron), 3)
        q_ev = chron.groupby("quartile")["pnl_c"].mean()
        weeks = g.copy()
        weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
        weekly_ev = weeks.groupby("week")["pnl_c"].mean()
        rows.append(
            {
                **dict(zip(group_cols, keys, strict=True)),
                "trades": len(g),
                "hit_rate": g["hit80"].mean(),
                "breakeven_hit_rate": g["entry_c"].mean() / TARGET_C,
                "avg_entry_c": g["entry_c"].mean(),
                "avg_pnl_c": g["pnl_c"].mean(),
                "min_quartile_ev_c": float(q_ev.min()),
                "quartile_ev_c": ",".join(f"{x:.2f}" for x in q_ev),
                "positive_weeks": int((weekly_ev > 0).sum()),
                "weeks": len(weekly_ev),
                "min_week_ev_c": float(weekly_ev.min()),
                "settle_win_rate": g["settle_win"].mean(),
                "avg_entry_minute": g["minute"].mean(),
                "avg_signal_dist_below_ema_c": g["signal_dist_below_ema_c"].mean(),
                "avg_bars_since_touch": g["signal_bars_since_touch"].mean(),
                "avg_drop5_c": g["drop5_c"].mean(),
            }
        )
    return pd.DataFrame(rows).sort_values(["avg_pnl_c", "trades"], ascending=[False, False])


def summarize(entries: pd.DataFrame) -> pd.DataFrame:
    if entries.empty:
        return pd.DataFrame()
    rows = []
    for keys, g in entries.groupby(
        ["rule", "side", "min_dist_c", "min_bars_since_touch", "min_drop_c", "recent_high_window"],
        observed=True,
    ):
        if len(g) < MIN_TRADES:
            continue
        rule, side, min_dist, min_bars, min_drop, window = keys
        chron = g.sort_values("market_end").reset_index(drop=True)
        chron["quartile"] = np.minimum((np.arange(len(chron)) * 4) // len(chron), 3)
        q_ev = chron.groupby("quartile")["pnl_c"].mean()
        weeks = g.copy()
        weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
        weekly_ev = weeks.groupby("week")["pnl_c"].mean()
        rows.append(
            {
                "rule": rule,
                "side": side,
                "entry_c": g["entry_c"].median(),
                "target_c": TARGET_C,
                "trades": len(g),
                "hit_rate": g["hit80"].mean(),
                "breakeven_hit_rate": g["entry_c"].mean() / TARGET_C,
                "avg_pnl_c": g["pnl_c"].mean(),
                "min_quartile_ev_c": float(q_ev.min()),
                "quartile_ev_c": ",".join(f"{x:.2f}" for x in q_ev),
                "positive_weeks": int((weekly_ev > 0).sum()),
                "weeks": len(weekly_ev),
                "min_week_ev_c": float(weekly_ev.min()),
                "settle_win_rate": g["settle_win"].mean(),
                "avg_entry_minute": g["minute"].mean(),
                "avg_signal_dist_below_ema_c": g["signal_dist_below_ema_c"].mean(),
                "avg_entry_dist_below_ema_c": g["dist_below_ema_c"].mean(),
                "avg_bars_since_touch": g["bars_since_ema_touch"].mean(),
                "avg_drop_from_recent_high_c": g["drop_from_recent_high_c"].mean(),
                "min_dist_c": min_dist,
                "min_bars_since_touch": min_bars,
                "min_drop_c": min_drop,
                "recent_high_window": window,
            }
        )
    return pd.DataFrame(rows).sort_values(["avg_pnl_c", "trades"], ascending=[False, False])


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    featured = add_ema_features(load_rows())
    entries = build_diagnostic_entries(featured)
    summaries = [
        summarize_diagnostic(entries, ["side"]).assign(table="side"),
        summarize_diagnostic(entries, ["dist_bin"]).assign(table="dist_bin"),
        summarize_diagnostic(entries, ["bars_bin"]).assign(table="bars_bin"),
        summarize_diagnostic(entries, ["side", "dist_bin"]).assign(table="side_dist"),
        summarize_diagnostic(entries, ["dist_bin", "bars_bin"]).assign(table="dist_bars"),
        summarize_diagnostic(entries, ["side", "entry_c"]).assign(table="side_entry"),
    ]
    summary = pd.concat([s for s in summaries if not s.empty], ignore_index=True)
    robust = summary[
        (summary["avg_pnl_c"] > 0)
        & (summary["min_quartile_ev_c"] > 0)
        & (summary["positive_weeks"] >= summary["weeks"] - 2)
    ].copy()

    entries_path = REPORTS / "btc15_kalshi_ema_reversion_entries.parquet"
    summary_path = REPORTS / "btc15_kalshi_ema_reversion_summary.csv"
    top_path = REPORTS / "btc15_kalshi_ema_reversion_top.csv"
    entries.to_parquet(entries_path, index=False)
    summary.to_csv(summary_path, index=False)
    robust.to_csv(top_path, index=False)

    print(f"entries: {len(entries):,}")
    print(f"summary rows: {len(summary):,}")
    print(f"positive rows: {int((summary['avg_pnl_c'] > 0).sum()) if not summary.empty else 0:,}")
    print(f"robust rows: {len(robust):,}")
    print(f"wrote: {entries_path.relative_to(ROOT)}")
    print(f"wrote: {summary_path.relative_to(ROOT)}")
    print(f"wrote: {top_path.relative_to(ROOT)}")
    cols = [
        "table",
        "side",
        "dist_bin",
        "bars_bin",
        "entry_c",
        "trades",
        "hit_rate",
        "breakeven_hit_rate",
        "avg_entry_c",
        "avg_pnl_c",
        "min_quartile_ev_c",
        "positive_weeks",
        "weeks",
        "settle_win_rate",
        "avg_entry_minute",
        "avg_signal_dist_below_ema_c",
        "avg_bars_since_touch",
        "avg_drop_from_recent_high_c",
    ]
    print("\nTop 80c mean-reversion rows")
    existing_cols = [col for col in cols if col in summary.columns]
    print(summary[existing_cols].head(40).round(4).to_string(index=False) if not summary.empty else "none")


if __name__ == "__main__":
    main()
