#!/usr/bin/env python3
"""Research BTC15 first-5-minute beatdown/recovery setups with BTC spot context.

Question:
- If we wait through minutes 1-5, can early Kalshi bid/ask behavior plus BTC
  1-minute price action identify recoveries worth entering from minute 6-10?

Trade model:
- One trade per market-side.
- Entry is the first minute >= 6 where the side ask closes in the configured
  recovery band.
- Exit is a later bid-high target touch; otherwise score as full entry loss.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
MINUTES = REPORTS / "btc15_minute_candidates.parquet"
BTC_1M = ROOT / "data" / "external" / "btc_usd_1m_coinbase.parquet"

WAIT_MINUTES = 5
ENTRY_MINUTE_MIN = 6
ENTRY_MINUTE_MAX = 10
TARGETS_C = [60, 70, 80, 85, 90, 99]


def side_dir(side: str) -> int:
    return 1 if side == "YES" else -1


def add_btc_to_minutes(minutes: pd.DataFrame) -> pd.DataFrame:
    btc = pd.read_parquet(BTC_1M).copy()
    btc["ts"] = pd.to_datetime(btc["ts"], utc=True).astype("datetime64[ns, UTC]")
    btc = btc.rename(
        columns={
            "open": "btc_open",
            "high": "btc_high",
            "low": "btc_low",
            "close": "btc_close",
            "volume": "btc_volume",
        }
    ).sort_values("ts")

    out = minutes.copy()
    out["market_end"] = pd.to_datetime(out["market_end"], utc=True).astype("datetime64[ns, UTC]")
    out["market_start"] = out["market_end"] - pd.Timedelta(minutes=15)
    out["minute_end"] = out["market_start"] + pd.to_timedelta(out["minute"], unit="m")
    out = out.sort_values("minute_end")
    return pd.merge_asof(
        out,
        btc,
        left_on="minute_end",
        right_on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=45),
    )


def first5_features(df: pd.DataFrame) -> pd.DataFrame:
    early = df[df["minute"] <= WAIT_MINUTES].copy()
    early["green"] = early["body"] > 0
    early["low_rank"] = early.groupby(["kalshi_ticker", "side"])["ask_low"].rank(method="first")

    grouped = early.groupby(["kalshi_ticker", "market_end", "side"], observed=True)
    rows = grouped.agg(
        early_ask_low=("ask_low", "min"),
        early_ask_high=("ask_high", "max"),
        early_ask_close_min=("ask_close", "min"),
        early_ask_close_max=("ask_close", "max"),
        early_spread_mean=("spread", "mean"),
        early_spread_max=("spread", "max"),
        early_green_minutes=("green", "sum"),
        early_volume_sum=("vol", "sum"),
        early_oi_last=("oi", "last"),
        btc_open_1=("btc_open", "first"),
        btc_close_5=("btc_close", "last"),
        btc_high_1_5=("btc_high", "max"),
        btc_low_1_5=("btc_low", "min"),
        btc_volume_1_5=("btc_volume", "sum"),
    ).reset_index()

    close5 = (
        early[early["minute"] == WAIT_MINUTES][["kalshi_ticker", "side", "ask_close", "bid_close"]]
        .rename(columns={"ask_close": "ask_close_m5", "bid_close": "bid_close_m5"})
    )
    low_minute = (
        early.sort_values(["kalshi_ticker", "side", "ask_low", "minute"])
        .drop_duplicates(["kalshi_ticker", "side"])[["kalshi_ticker", "side", "minute"]]
        .rename(columns={"minute": "early_low_minute"})
    )
    rows = rows.merge(close5, on=["kalshi_ticker", "side"], how="left")
    rows = rows.merge(low_minute, on=["kalshi_ticker", "side"], how="left")

    direction = rows["side"].map(side_dir)
    rows["early_ask_range"] = rows["early_ask_high"] - rows["early_ask_low"]
    rows["early_reclaim_to_m5"] = rows["ask_close_m5"] - rows["early_ask_low"]
    rows["early_drop_to_low"] = rows["early_ask_high"] - rows["early_ask_low"]
    rows["btc_ret_1_5"] = rows["btc_close_5"] - rows["btc_open_1"]
    rows["signed_btc_ret_1_5"] = direction * rows["btc_ret_1_5"]
    rows["btc_range_1_5"] = rows["btc_high_1_5"] - rows["btc_low_1_5"]
    rows["signed_btc_close_pos_1_5"] = direction * (
        (rows["btc_close_5"] - rows["btc_low_1_5"])
        / rows["btc_range_1_5"].replace(0, np.nan)
    )
    return rows


def build_entries(df: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    later = df[(df["minute"] >= ENTRY_MINUTE_MIN) & (df["minute"] <= ENTRY_MINUTE_MAX)].copy()
    later = later.merge(features, on=["kalshi_ticker", "market_end", "side"], how="left")
    later["entry_price_c"] = later["ask_close"]
    later["target_85_hit"] = later["future_max_bid"] >= 85

    rows = []
    for low_max in [20, 25, 30, 35, 40]:
        for close_min, close_max in [(40, 65), (45, 65), (50, 70), (55, 75)]:
            mask = (
                (later["early_ask_low"] <= low_max)
                & (later["entry_price_c"] >= close_min)
                & (later["entry_price_c"] < close_max)
                & (later["ask_close"] > later["ask_close_m5"])
                & (later["body"] > 0)
            )
            candidates = later[mask].sort_values(["market_end", "kalshi_ticker", "side", "minute"])
            first = candidates.drop_duplicates(["kalshi_ticker", "side"], keep="first").copy()
            if first.empty:
                continue
            first["rule"] = f"low<={low_max}_close{close_min}-{close_max}_green"
            rows.append(first)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def summarize(entries: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if entries.empty:
        return pd.DataFrame()
    for rule, d0 in entries.groupby("rule", observed=True):
        for side_filter in ["BOTH", "YES", "NO"]:
            d = d0 if side_filter == "BOTH" else d0[d0["side"] == side_filter]
            if d.empty:
                continue
            for target_c in TARGETS_C:
                d = d.copy()
                d["hit"] = d["future_max_bid"] >= target_c
                d["pnl_c"] = np.where(d["hit"], target_c - d["entry_price_c"], -d["entry_price_c"])
                chron = d.sort_values("market_end").reset_index(drop=True)
                if len(chron) >= 4:
                    chron["quartile"] = pd.qcut(
                        chron.index, q=4, labels=["q1", "q2", "q3", "q4"], duplicates="drop"
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
                rows.append(
                    {
                        "rule": rule,
                        "side_filter": side_filter,
                        "target_c": target_c,
                        "trades": len(d),
                        "avg_entry_c": d["entry_price_c"].mean(),
                        "median_entry_minute": d["minute"].median(),
                        "hit_rate": d["hit"].mean(),
                        "breakeven_hit_rate": d["entry_price_c"].mean() / target_c,
                        "avg_pnl_c": d["pnl_c"].mean(),
                        "total_pnl_c": d["pnl_c"].sum(),
                        "min_quartile_ev_c": min_q_ev,
                        "quartile_ev_c": q_ev_text,
                        "positive_weeks": int((weekly_ev > 0).sum()),
                        "weeks": len(weekly_ev),
                        "min_week_ev_c": float(weekly_ev.min()),
                        "avg_early_low_c": d["early_ask_low"].mean(),
                        "avg_early_reclaim_c": d["early_reclaim_to_m5"].mean(),
                        "avg_signed_btc_ret_1_5": d["signed_btc_ret_1_5"].mean(),
                        "avg_btc_range_1_5": d["btc_range_1_5"].mean(),
                    }
                )
    return pd.DataFrame(rows)


def btc_condition_slices(entries: pd.DataFrame) -> pd.DataFrame:
    if entries.empty:
        return pd.DataFrame()
    d = entries.copy()
    d["btc_signed_bin"] = pd.cut(
        d["signed_btc_ret_1_5"],
        bins=[-np.inf, -75, -25, 25, 75, np.inf],
        labels=["against>75", "against25-75", "flat", "with25-75", "with>75"],
    )
    d["early_low_bin"] = pd.cut(
        d["early_ask_low"],
        bins=[-np.inf, 15, 20, 25, 30, 35, np.inf],
        labels=["<=15", "15-20", "20-25", "25-30", "30-35", "35+"],
    )
    d["target_c"] = 85
    d["hit"] = d["future_max_bid"] >= d["target_c"]
    d["pnl_c"] = np.where(d["hit"], d["target_c"] - d["entry_price_c"], -d["entry_price_c"])
    return (
        d.groupby(["rule", "side", "btc_signed_bin", "early_low_bin"], observed=True)
        .agg(
            trades=("hit", "size"),
            hit85=("hit", "mean"),
            avg_entry_c=("entry_price_c", "mean"),
            avg_pnl85_c=("pnl_c", "mean"),
            median_entry_minute=("minute", "median"),
        )
        .reset_index()
        .sort_values(["avg_pnl85_c", "trades"], ascending=[False, False])
    )


def conditioned_rule_summary(entries: pd.DataFrame) -> pd.DataFrame:
    if entries.empty:
        return pd.DataFrame()
    d = entries.copy()
    d["btc_signed_bin"] = pd.cut(
        d["signed_btc_ret_1_5"],
        bins=[-np.inf, -75, -25, 25, 75, np.inf],
        labels=["against>75", "against25-75", "flat", "with25-75", "with>75"],
    )
    d["early_low_bin"] = pd.cut(
        d["early_ask_low"],
        bins=[-np.inf, 15, 20, 25, 30, 35, np.inf],
        labels=["<=15", "15-20", "20-25", "25-30", "30-35", "35+"],
    )

    rows = []
    group_cols = ["rule", "side", "btc_signed_bin", "early_low_bin"]
    for keys, g0 in d.groupby(group_cols, observed=True):
        if len(g0) < 30:
            continue
        for target_c in TARGETS_C:
            g = g0.copy()
            g["hit"] = g["future_max_bid"] >= target_c
            g["pnl_c"] = np.where(g["hit"], target_c - g["entry_price_c"], -g["entry_price_c"])
            chron = g.sort_values("market_end").reset_index(drop=True)
            if len(chron) >= 4:
                chron["quartile"] = pd.qcut(
                    chron.index, q=4, labels=["q1", "q2", "q3", "q4"], duplicates="drop"
                )
                q_ev = chron.groupby("quartile", observed=True)["pnl_c"].mean()
                min_q_ev = float(q_ev.min())
                q_ev_text = ",".join(f"{x:.2f}" for x in q_ev)
            else:
                min_q_ev = np.nan
                q_ev_text = ""
            weeks = g.copy()
            weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
            weekly_ev = weeks.groupby("week")["pnl_c"].mean()
            rows.append(
                {
                    "rule": keys[0],
                    "side": keys[1],
                    "btc_signed_bin": keys[2],
                    "early_low_bin": keys[3],
                    "target_c": target_c,
                    "trades": len(g),
                    "avg_entry_c": g["entry_price_c"].mean(),
                    "hit_rate": g["hit"].mean(),
                    "breakeven_hit_rate": g["entry_price_c"].mean() / target_c,
                    "avg_pnl_c": g["pnl_c"].mean(),
                    "min_quartile_ev_c": min_q_ev,
                    "quartile_ev_c": q_ev_text,
                    "positive_weeks": int((weekly_ev > 0).sum()),
                    "weeks": len(weekly_ev),
                    "min_week_ev_c": float(weekly_ev.min()),
                    "median_entry_minute": g["minute"].median(),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    minutes = pd.read_parquet(MINUTES)
    minutes = add_btc_to_minutes(minutes)
    features = first5_features(minutes)
    entries = build_entries(minutes, features)
    summary = summarize(entries)
    slices = btc_condition_slices(entries)
    conditioned = conditioned_rule_summary(entries)

    entries_path = REPORTS / "btc15_first5_btc_recovery_entries.parquet"
    summary_path = REPORTS / "btc15_first5_btc_recovery_summary.csv"
    slices_path = REPORTS / "btc15_first5_btc_recovery_btc_slices.csv"
    conditioned_path = REPORTS / "btc15_first5_btc_recovery_conditioned_summary.csv"
    entries.to_parquet(entries_path, index=False)
    summary.to_csv(summary_path, index=False)
    slices.to_csv(slices_path, index=False)
    conditioned.to_csv(conditioned_path, index=False)

    robust = summary[
        (summary["trades"] >= 100)
        & (summary["avg_pnl_c"] > 0)
        & (summary["min_quartile_ev_c"] > 0)
        & (summary["positive_weeks"] >= (summary["weeks"] - 1))
    ].sort_values(["avg_pnl_c", "trades"], ascending=[False, False])

    print(f"minute rows: {len(minutes):,}")
    print(f"entry rows: {len(entries):,}")
    print(f"wrote: {entries_path.relative_to(ROOT)}")
    print(f"wrote: {summary_path.relative_to(ROOT)}")
    print(f"wrote: {slices_path.relative_to(ROOT)}")
    print(f"wrote: {conditioned_path.relative_to(ROOT)}")
    print("\nTop robust recovery rules")
    if robust.empty:
        print("none")
    else:
        cols = [
            "rule", "side_filter", "target_c", "trades", "avg_entry_c", "hit_rate",
            "breakeven_hit_rate", "avg_pnl_c", "min_quartile_ev_c",
            "positive_weeks", "weeks", "avg_signed_btc_ret_1_5",
        ]
        print(robust[cols].head(30).round(4).to_string(index=False))

    print("\nTop BTC slices at 85c target")
    if slices.empty:
        print("none")
    else:
        print(slices[slices["trades"] >= 30].head(30).round(4).to_string(index=False))

    print("\nTop conditioned rules")
    if conditioned.empty:
        print("none")
    else:
        robust_conditioned = conditioned[
            (conditioned["trades"] >= 50)
            & (conditioned["avg_pnl_c"] > 0)
            & (conditioned["min_quartile_ev_c"] > 0)
            & (conditioned["positive_weeks"] >= (conditioned["weeks"] - 2))
        ].sort_values(["avg_pnl_c", "trades"], ascending=[False, False])
        cols = [
            "rule", "side", "btc_signed_bin", "early_low_bin", "target_c",
            "trades", "avg_entry_c", "hit_rate", "breakeven_hit_rate",
            "avg_pnl_c", "min_quartile_ev_c", "positive_weeks", "weeks",
            "median_entry_minute",
        ]
        if robust_conditioned.empty:
            print("none")
        else:
            print(robust_conditioned[cols].head(30).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
