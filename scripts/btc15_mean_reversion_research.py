#!/usr/bin/env python3
"""Research BTC15 mean-reversion entries around BTC EMA21 extensions.

Setup being tested:
- Use only completed BTC 1m candles inside a BTC15 market.
- If BTC closes far above EMA21, only consider NO.
- If BTC closes far below EMA21, only consider YES.
- Entry is selective: after the stretch signal, BTC must be at least as
  extended, and the opposite Kalshi side must trade down into 20-25c during
  the signal bar or the next bar.
- Evaluate scalp targets and final settlement without wiring this into live
  trading.

The local minute-candidate dataset currently contains minutes 1-10 only, so
this research intentionally tests entries inside that window.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
MINUTES = REPORTS / "btc15_minute_candidates.parquet"
TRAINING = ROOT / "data" / "kalshi_btc15_t600_training.parquet"
BTC_1M = ROOT / "data" / "external" / "btc_usd_1m_coinbase.parquet"

EMA_SPAN = 21
ENTRY_LIMITS_C = range(20, 26)
TARGETS_C = [45, 50, 55, 60, 65, 70, 75, 80, 85, 99]
MIN_SIGNAL_MINUTE = 1
MAX_ENTRY_MINUTE = 10
ENTRY_WINDOW_BARS = 2
MIN_EXTENSION_BPS = [10, 15, 20, 25, 30, 40, 50]
MIN_FURTHER_EXTENSION_BPS = [0, 5, 10, 15]
MIN_TRADES = 25


def opposite_side(ema_dist: pd.Series) -> pd.Series:
    return np.where(ema_dist > 0, "NO", "YES")


def load_btc() -> pd.DataFrame:
    btc = pd.read_parquet(BTC_1M).copy()
    btc["ts"] = pd.to_datetime(btc["ts"], utc=True).astype("datetime64[ns, UTC]")
    btc = btc.sort_values("ts").rename(
        columns={
            "open": "btc_open",
            "high": "btc_high",
            "low": "btc_low",
            "close": "btc_close",
            "volume": "btc_volume",
        }
    )
    btc["btc_ema21"] = btc["btc_close"].ewm(span=EMA_SPAN, adjust=False).mean()
    btc["btc_ema21_slope_3m"] = btc["btc_ema21"].diff(3)
    btc["ema_dist"] = btc["btc_close"] - btc["btc_ema21"]
    btc["ema_dist_abs"] = btc["ema_dist"].abs()
    btc["ema_dist_bps"] = btc["ema_dist"] / btc["btc_close"] * 10_000
    btc["ema_dist_abs_bps"] = btc["ema_dist_bps"].abs()
    btc["signal_side"] = opposite_side(btc["ema_dist"])
    return btc


def load_minutes() -> pd.DataFrame:
    minutes = pd.read_parquet(MINUTES).copy()
    minutes["market_end"] = pd.to_datetime(minutes["market_end"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    minutes["market_start"] = minutes["market_end"] - pd.Timedelta(minutes=15)
    minutes["minute_end"] = minutes["market_start"] + pd.to_timedelta(
        minutes["minute"], unit="m"
    )
    outcomes = pd.read_parquet(TRAINING)[
        ["kalshi_ticker", "kalshi_outcome_up", "market_start", "market_end"]
    ].copy()
    outcomes["market_start"] = pd.to_datetime(outcomes["market_start"], utc=True)
    outcomes["market_end"] = pd.to_datetime(outcomes["market_end"], utc=True)
    minutes = minutes.merge(
        outcomes[["kalshi_ticker", "kalshi_outcome_up"]],
        on="kalshi_ticker",
        how="left",
    )
    minutes["settle_win"] = np.where(
        minutes["side"].eq("YES"),
        minutes["kalshi_outcome_up"],
        1 - minutes["kalshi_outcome_up"],
    )
    return minutes


def add_btc_context(minutes: pd.DataFrame) -> pd.DataFrame:
    btc = load_btc()
    out = minutes.sort_values("minute_end")
    out = pd.merge_asof(
        out,
        btc,
        left_on="minute_end",
        right_on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=45),
    )
    return out.sort_values(["market_end", "kalshi_ticker", "side", "minute"])


def add_future_btc_context(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values(["kalshi_ticker", "side", "minute"], ascending=[True, True, False]).copy()
    grouped = d.groupby(["kalshi_ticker", "side"], observed=True, sort=False)
    d["future_min_abs_ema_bps"] = grouped["ema_dist_abs_bps"].cummin()
    d["future_min_abs_ema"] = grouped["ema_dist_abs"].cummin()
    d["future_touches_ema21"] = d["future_min_abs_ema_bps"] <= 2
    return d.sort_values(["market_end", "kalshi_ticker", "side", "minute"])


def build_entries(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    usable = df[
        df["minute"].between(MIN_SIGNAL_MINUTE, MAX_ENTRY_MINUTE)
        & df["btc_close"].notna()
        & df["kalshi_outcome_up"].notna()
    ].copy()

    for min_ext_bps in MIN_EXTENSION_BPS:
        signal_rows = usable[
            (usable["ema_dist_abs_bps"] >= min_ext_bps)
            & (usable["side"] == usable["signal_side"])
        ].copy()
        if signal_rows.empty:
            continue

        signals = (
            signal_rows.sort_values(["market_end", "kalshi_ticker", "side", "minute"])
            .drop_duplicates(["kalshi_ticker", "side"], keep="first")
            [
                [
                    "kalshi_ticker",
                    "side",
                    "minute",
                    "btc_close",
                    "btc_ema21",
                    "btc_ema21_slope_3m",
                    "ema_dist",
                    "ema_dist_abs",
                    "ema_dist_bps",
                    "ema_dist_abs_bps",
                ]
            ]
            .rename(
                columns={
                    "minute": "signal_minute",
                    "btc_close": "signal_btc_close",
                    "btc_ema21": "signal_btc_ema21",
                    "btc_ema21_slope_3m": "signal_ema21_slope_3m",
                    "ema_dist": "signal_ema_dist",
                    "ema_dist_abs": "signal_ema_dist_abs",
                    "ema_dist_bps": "signal_ema_dist_bps",
                    "ema_dist_abs_bps": "signal_ema_dist_abs_bps",
                }
            )
        )

        candidates = usable.merge(signals, on=["kalshi_ticker", "side"], how="inner")
        candidates = candidates[
            (candidates["minute"] >= candidates["signal_minute"])
            & (candidates["minute"] < candidates["signal_minute"] + ENTRY_WINDOW_BARS)
            & (candidates["minute"] <= MAX_ENTRY_MINUTE)
        ].copy()
        candidates["extension_after_signal_bps"] = (
            candidates["ema_dist_abs_bps"] - candidates["signal_ema_dist_abs_bps"]
        )
        candidates["same_extension_direction"] = np.sign(candidates["ema_dist"]) == np.sign(
            candidates["signal_ema_dist"]
        )

        for further_bps in MIN_FURTHER_EXTENSION_BPS:
            stretched = candidates[
                candidates["same_extension_direction"]
                & (candidates["extension_after_signal_bps"] >= further_bps)
            ].copy()
            if stretched.empty:
                continue
            for entry_c in ENTRY_LIMITS_C:
                fills = stretched[
                    (stretched["ask_low"] <= entry_c)
                    & (stretched["ask_close"] <= entry_c + 10)
                ].copy()
                if fills.empty:
                    continue
                first = (
                    fills.sort_values(["market_end", "kalshi_ticker", "side", "minute"])
                    .drop_duplicates(["kalshi_ticker", "side"], keep="first")
                    .copy()
                )
                first["rule"] = (
                    f"ext{min_ext_bps}bps_further{further_bps}bps_entry{entry_c}c"
                )
                first["min_signal_ext_bps"] = min_ext_bps
                first["min_further_ext_bps"] = further_bps
                first["entry_c"] = entry_c
                first["entry_price_c"] = entry_c
                rows.append(first)

    if not rows:
        return pd.DataFrame()
    entries = pd.concat(rows, ignore_index=True)
    entries["entry_to_ema_bps"] = entries["ema_dist_abs_bps"]
    entries["entry_to_signal_move_bps"] = entries["extension_after_signal_bps"]
    entries["entry_minutes_after_signal"] = entries["minute"] - entries["signal_minute"]
    entries["settle_win"] = entries["settle_win"].astype(int)
    return entries.sort_values(["market_end", "kalshi_ticker", "side", "rule"])


def add_bins(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries.copy()
    d["signal_ext_bin"] = pd.cut(
        d["signal_ema_dist_abs_bps"],
        bins=[0, 10, 20, 30, 40, 50, 75, np.inf],
        labels=["0-10", "10-20", "20-30", "30-40", "40-50", "50-75", "75+"],
    )
    d["entry_ext_bin"] = pd.cut(
        d["entry_to_ema_bps"],
        bins=[0, 10, 20, 30, 40, 50, 75, np.inf],
        labels=["0-10", "10-20", "20-30", "30-40", "40-50", "50-75", "75+"],
    )
    d["entry_minute_bin"] = pd.cut(
        d["minute"],
        bins=[0, 2, 4, 6, 8, 10],
        labels=["m1-2", "m3-4", "m5-6", "m7-8", "m9-10"],
    )
    d["ema_slope_bin"] = pd.cut(
        d["signal_ema21_slope_3m"],
        bins=[-np.inf, -75, -25, 25, 75, np.inf],
        labels=["down>75", "down25-75", "flat", "up25-75", "up>75"],
    )
    return d


def summarize(entries: pd.DataFrame, group_cols: list[str], min_trades: int) -> pd.DataFrame:
    if entries.empty:
        return pd.DataFrame()
    rows = []
    for keys, g0 in entries.groupby(group_cols, observed=True):
        if len(g0) < min_trades:
            continue
        if not isinstance(keys, tuple):
            keys = (keys,)
        base = dict(zip(group_cols, keys, strict=True))
        for target_c in TARGETS_C:
            g = g0.copy()
            g["target_c"] = target_c
            g["hit_target"] = g["future_max_bid"] >= target_c
            g["pnl_c"] = np.where(g["hit_target"], target_c - g["entry_c"], -g["entry_c"])
            chron = g.sort_values("market_end").reset_index(drop=True)
            chron["quartile"] = np.minimum((np.arange(len(chron)) * 4) // len(chron), 3)
            q_ev = chron.groupby("quartile", observed=True)["pnl_c"].mean()
            weeks = g.copy()
            weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
            weekly_ev = weeks.groupby("week")["pnl_c"].mean()
            rows.append(
                {
                    **base,
                    "target_c": target_c,
                    "trades": len(g),
                    "hit_rate": g["hit_target"].mean(),
                    "breakeven_hit_rate": g["entry_c"].mean() / target_c,
                    "avg_pnl_c": g["pnl_c"].mean(),
                    "min_quartile_ev_c": float(q_ev.min()),
                    "quartile_ev_c": ",".join(f"{x:.2f}" for x in q_ev),
                    "positive_weeks": int((weekly_ev > 0).sum()),
                    "weeks": len(weekly_ev),
                    "min_week_ev_c": float(weekly_ev.min()),
                    "settle_win_rate": g["settle_win"].mean(),
                    "touch_ema_after_entry": g["future_touches_ema21"].mean(),
                    "avg_entry_minute": g["minute"].mean(),
                    "avg_signal_ext_bps": g["signal_ema_dist_abs_bps"].mean(),
                    "avg_entry_ext_bps": g["entry_to_ema_bps"].mean(),
                    "avg_further_ext_bps": g["entry_to_signal_move_bps"].mean(),
                    "yes_share": g["side"].eq("YES").mean(),
                }
            )
    return pd.DataFrame(rows)


def select_best(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return summary
    return summary[
        (summary["trades"] >= MIN_TRADES)
        & (summary["target_c"].between(55, 85))
        & (summary["avg_pnl_c"] > 0)
        & (summary["min_quartile_ev_c"] > 0)
        & (summary["positive_weeks"] >= summary["weeks"] - 2)
    ].sort_values(["avg_pnl_c", "trades"], ascending=[False, False])


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    minutes = add_future_btc_context(add_btc_context(load_minutes()))
    entries = add_bins(build_entries(minutes))

    entries_path = REPORTS / "btc15_mean_reversion_entries.parquet"
    summary_path = REPORTS / "btc15_mean_reversion_summary.csv"
    top_path = REPORTS / "btc15_mean_reversion_top.csv"
    detail_path = REPORTS / "btc15_mean_reversion_details.md"

    entries.to_parquet(entries_path, index=False)
    summary = summarize(entries, ["rule", "side"], min_trades=MIN_TRADES)
    if not summary.empty:
        summary = summary.assign(table="rule_side")
    top = select_best(summary)
    summary.to_csv(summary_path, index=False)
    top.to_csv(top_path, index=False)

    with detail_path.open("w") as fh:
        fh.write("# BTC15 EMA21 Mean-Reversion Research\n\n")
        fh.write("This is isolated research and is not imported by the live BTC bot.\n\n")
        fh.write("Rules tested:\n")
        fh.write("- BTC close above EMA21 maps to NO; BTC close below EMA21 maps to YES.\n")
        fh.write("- Signal and entry use completed BTC 1m candles only.\n")
        fh.write("- Entry limit is 20-25c and must occur on the signal bar or following bar.\n")
        fh.write("- The entry candle must remain in the same extension direction and satisfy the configured further-extension threshold.\n\n")
        fh.write(f"Entries: {len(entries):,}\n\n")
        fh.write("## Top Rules\n\n")
        if top.empty:
            fh.write("No robust positive rules found with the current filters.\n")
        else:
            cols = [
                "table",
                "rule",
                "side",
                "target_c",
                "trades",
                "hit_rate",
                "breakeven_hit_rate",
                "avg_pnl_c",
                "min_quartile_ev_c",
                "positive_weeks",
                "weeks",
                "settle_win_rate",
                "touch_ema_after_entry",
                "avg_entry_minute",
                "avg_signal_ext_bps",
                "avg_entry_ext_bps",
            ]
            fh.write(top[cols].head(40).round(4).to_markdown(index=False))
            fh.write("\n")

    print(f"entries: {len(entries):,}")
    print(f"summary rows: {len(summary):,}")
    print(f"top rows: {len(top):,}")
    print(f"wrote: {entries_path.relative_to(ROOT)}")
    print(f"wrote: {summary_path.relative_to(ROOT)}")
    print(f"wrote: {top_path.relative_to(ROOT)}")
    print(f"wrote: {detail_path.relative_to(ROOT)}")
    print("\nTop robust mean-reversion rules")
    if top.empty:
        print("none")
    else:
        cols = [
            "table",
            "rule",
            "side",
            "target_c",
            "trades",
            "hit_rate",
            "breakeven_hit_rate",
            "avg_pnl_c",
            "min_quartile_ev_c",
            "positive_weeks",
            "weeks",
            "settle_win_rate",
            "touch_ema_after_entry",
            "avg_entry_minute",
        ]
        print(top[cols].head(40).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
