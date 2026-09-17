#!/usr/bin/env python3
"""BTC15 decision-window research with BTC 15m EMA21 context.

This tests the bot shape Joshua described:
- Wait through the first 5 or 6 minutes.
- Use granular Kalshi bid/ask behavior plus BTC chart context.
- Buy a recovered side from below.
- Scalp to 65-85c instead of requiring 99c.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
MINUTES = REPORTS / "btc15_minute_candidates.parquet"
BTC_1M = ROOT / "data" / "external" / "btc_usd_1m_coinbase.parquet"

DECISION_WINDOWS = [5, 6]
ENTRY_LAST_MINUTE = 10
TARGETS_C = [65, 70, 75, 80, 85]


def side_dir(side: str) -> int:
    return 1 if side == "YES" else -1


def load_btc() -> pd.DataFrame:
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
    btc["btc_ema21_1m"] = btc["btc_close"].ewm(span=21, adjust=False).mean()

    btc15 = (
        btc.set_index("ts")
        .resample("15min", label="right", closed="right")
        .agg(
            btc15_open=("btc_open", "first"),
            btc15_high=("btc_high", "max"),
            btc15_low=("btc_low", "min"),
            btc15_close=("btc_close", "last"),
            btc15_volume=("btc_volume", "sum"),
        )
        .dropna()
        .reset_index()
    )
    btc15["btc15_ema21"] = btc15["btc15_close"].ewm(span=21, adjust=False).mean()
    btc15["btc15_slope_3"] = btc15["btc15_ema21"].diff(3)
    return btc, btc15


def add_btc_context(minutes: pd.DataFrame) -> pd.DataFrame:
    btc, btc15 = load_btc()
    out = minutes.copy()
    out["market_end"] = pd.to_datetime(out["market_end"], utc=True).astype("datetime64[ns, UTC]")
    out["market_start"] = out["market_end"] - pd.Timedelta(minutes=15)
    out["minute_end"] = out["market_start"] + pd.to_timedelta(out["minute"], unit="m")
    out = out.sort_values("minute_end")

    out = pd.merge_asof(
        out,
        btc,
        left_on="minute_end",
        right_on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=45),
    )
    out = pd.merge_asof(
        out.sort_values("minute_end"),
        btc15.sort_values("ts"),
        left_on="minute_end",
        right_on="ts",
        direction="backward",
        tolerance=pd.Timedelta(minutes=30),
        suffixes=("", "_15m"),
    )
    return out


def decision_features(df: pd.DataFrame, decision_minute: int) -> pd.DataFrame:
    early = df[df["minute"] <= decision_minute].copy()
    early["green"] = early["body"] > 0
    grouped = early.groupby(["kalshi_ticker", "market_end", "side"], observed=True)
    feat = grouped.agg(
        early_ask_low=("ask_low", "min"),
        early_ask_high=("ask_high", "max"),
        early_bid_high=("bid_high", "max"),
        early_ask_close_min=("ask_close", "min"),
        early_ask_close_max=("ask_close", "max"),
        early_spread_mean=("spread", "mean"),
        early_spread_max=("spread", "max"),
        early_green_minutes=("green", "sum"),
        early_volume_sum=("vol", "sum"),
        btc_open_first=("btc_open", "first"),
        btc_close_decision=("btc_close", "last"),
        btc_high_window=("btc_high", "max"),
        btc_low_window=("btc_low", "min"),
        btc_volume_window=("btc_volume", "sum"),
        btc_ema21_1m_decision=("btc_ema21_1m", "last"),
        btc15_ema21_decision=("btc15_ema21", "last"),
        btc15_slope_3=("btc15_slope_3", "last"),
    ).reset_index()

    close_decision = (
        early[early["minute"] == decision_minute][["kalshi_ticker", "side", "ask_close", "bid_close"]]
        .rename(
            columns={
                "ask_close": f"ask_close_m{decision_minute}",
                "bid_close": f"bid_close_m{decision_minute}",
            }
        )
    )
    low_minute = (
        early.sort_values(["kalshi_ticker", "side", "ask_low", "minute"])
        .drop_duplicates(["kalshi_ticker", "side"])[["kalshi_ticker", "side", "minute"]]
        .rename(columns={"minute": "early_low_minute"})
    )
    feat = feat.merge(close_decision, on=["kalshi_ticker", "side"], how="left")
    feat = feat.merge(low_minute, on=["kalshi_ticker", "side"], how="left")

    direction = feat["side"].map(side_dir)
    feat["decision_minute"] = decision_minute
    feat["early_ask_range"] = feat["early_ask_high"] - feat["early_ask_low"]
    feat["early_reclaim_to_decision"] = feat[f"ask_close_m{decision_minute}"] - feat["early_ask_low"]
    feat["btc_ret_window"] = feat["btc_close_decision"] - feat["btc_open_first"]
    feat["signed_btc_ret_window"] = direction * feat["btc_ret_window"]
    feat["btc_range_window"] = feat["btc_high_window"] - feat["btc_low_window"]
    feat["signed_dist_ema21_15m"] = direction * (
        feat["btc_close_decision"] - feat["btc15_ema21_decision"]
    )
    feat["signed_dist_ema21_1m"] = direction * (
        feat["btc_close_decision"] - feat["btc_ema21_1m_decision"]
    )
    feat["signed_ema21_slope_15m"] = direction * feat["btc15_slope_3"]
    return feat


def build_entries(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for decision_minute in DECISION_WINDOWS:
        feat = decision_features(df, decision_minute)
        later = df[(df["minute"] > decision_minute) & (df["minute"] <= ENTRY_LAST_MINUTE)].copy()
        later = later.merge(feat, on=["kalshi_ticker", "market_end", "side"], how="left")
        decision_close_col = f"ask_close_m{decision_minute}"
        later["entry_price_c"] = later["ask_close"]

        for low_min, low_max in [(0, 20), (15, 25), (20, 30), (25, 35), (30, 40)]:
            for close_min, close_max in [(35, 55), (40, 60), (45, 65), (50, 70)]:
                mask = (
                    (later["early_ask_low"] > low_min)
                    & (later["early_ask_low"] <= low_max)
                    & (later["entry_price_c"] >= close_min)
                    & (later["entry_price_c"] < close_max)
                    & (later["ask_close"] > later[decision_close_col])
                    & (later["body"] > 0)
                )
                first = (
                    later[mask]
                    .sort_values(["market_end", "kalshi_ticker", "side", "minute"])
                    .drop_duplicates(["kalshi_ticker", "side"], keep="first")
                    .copy()
                )
                if first.empty:
                    continue
                first["rule"] = (
                    f"m{decision_minute}_low{low_min}-{low_max}_"
                    f"close{close_min}-{close_max}_green"
                )
                rows.append(first)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def add_bins(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries.copy()
    d["btc_ret_bin"] = pd.cut(
        d["signed_btc_ret_window"],
        bins=[-np.inf, -100, -50, -20, 20, 50, 100, np.inf],
        labels=["against>100", "against50-100", "against20-50", "flat", "with20-50", "with50-100", "with>100"],
    )
    d["ema15_bin"] = pd.cut(
        d["signed_dist_ema21_15m"],
        bins=[-np.inf, -150, -50, 0, 50, 150, np.inf],
        labels=["below>150", "below50-150", "below0-50", "above0-50", "above50-150", "above>150"],
    )
    d["ema15_slope_bin"] = pd.cut(
        d["signed_ema21_slope_15m"],
        bins=[-np.inf, -75, -25, 25, 75, np.inf],
        labels=["slope_against>75", "slope_against25-75", "slope_flat", "slope_with25-75", "slope_with>75"],
    )
    return d


def summarize(entries: pd.DataFrame, group_cols: list[str], min_group: int = 20) -> pd.DataFrame:
    rows = []
    if entries.empty:
        return pd.DataFrame()
    grouped = entries.groupby(group_cols, observed=True)
    for keys, g0 in grouped:
        if len(g0) < min_group:
            continue
        if not isinstance(keys, tuple):
            keys = (keys,)
        base = dict(zip(group_cols, keys, strict=True))
        for target_c in TARGETS_C:
            g = g0.copy()
            g["hit"] = g["future_max_bid"] >= target_c
            g["pnl_c"] = np.where(g["hit"], target_c - g["entry_price_c"], -g["entry_price_c"])
            chron = g.sort_values("market_end").reset_index(drop=True)
            chron["quartile"] = pd.qcut(
                chron.index, q=4, labels=["q1", "q2", "q3", "q4"], duplicates="drop"
            )
            q_ev = chron.groupby("quartile", observed=True)["pnl_c"].mean()
            weeks = g.copy()
            weeks["week"] = weeks["market_end"].dt.strftime("%G-W%V")
            weekly_ev = weeks.groupby("week")["pnl_c"].mean()
            rows.append(
                {
                    **base,
                    "target_c": target_c,
                    "trades": len(g),
                    "avg_entry_c": g["entry_price_c"].mean(),
                    "median_entry_minute": g["minute"].median(),
                    "hit_rate": g["hit"].mean(),
                    "breakeven_hit_rate": g["entry_price_c"].mean() / target_c,
                    "avg_pnl_c": g["pnl_c"].mean(),
                    "min_quartile_ev_c": float(q_ev.min()),
                    "quartile_ev_c": ",".join(f"{x:.2f}" for x in q_ev),
                    "positive_weeks": int((weekly_ev > 0).sum()),
                    "weeks": len(weekly_ev),
                    "min_week_ev_c": float(weekly_ev.min()),
                    "avg_signed_btc_ret_window": g["signed_btc_ret_window"].mean(),
                    "avg_signed_dist_ema21_15m": g["signed_dist_ema21_15m"].mean(),
                    "avg_signed_ema21_slope_15m": g["signed_ema21_slope_15m"].mean(),
                    "avg_early_low_c": g["early_ask_low"].mean(),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    minutes = pd.read_parquet(MINUTES)
    minutes = add_btc_context(minutes)
    entries = add_bins(build_entries(minutes))

    plain = summarize(entries, ["rule", "side"], min_group=40)
    btc_ema = summarize(entries, ["rule", "side", "btc_ret_bin", "ema15_bin"], min_group=25)
    btc_ema_slope = summarize(
        entries,
        ["rule", "side", "btc_ret_bin", "ema15_bin", "ema15_slope_bin"],
        min_group=20,
    )

    entries_path = REPORTS / "btc15_ema_decision_entries.parquet"
    plain_path = REPORTS / "btc15_ema_decision_summary.csv"
    btc_ema_path = REPORTS / "btc15_ema_decision_btc_ema_summary.csv"
    btc_ema_slope_path = REPORTS / "btc15_ema_decision_btc_ema_slope_summary.csv"
    entries.to_parquet(entries_path, index=False)
    plain.to_csv(plain_path, index=False)
    btc_ema.to_csv(btc_ema_path, index=False)
    btc_ema_slope.to_csv(btc_ema_slope_path, index=False)

    robust = btc_ema[
        (btc_ema["target_c"].between(65, 85))
        & (btc_ema["trades"] >= 40)
        & (btc_ema["avg_pnl_c"] > 0)
        & (btc_ema["min_quartile_ev_c"] > 0)
        & (btc_ema["positive_weeks"] >= btc_ema["weeks"] - 2)
    ].sort_values(["avg_pnl_c", "trades"], ascending=[False, False])

    print(f"entries: {len(entries):,}")
    print(f"wrote: {entries_path.relative_to(ROOT)}")
    print(f"wrote: {plain_path.relative_to(ROOT)}")
    print(f"wrote: {btc_ema_path.relative_to(ROOT)}")
    print(f"wrote: {btc_ema_slope_path.relative_to(ROOT)}")
    print("\nTop BTC+EMA scalp rules")
    if robust.empty:
        print("none")
    else:
        cols = [
            "rule",
            "side",
            "btc_ret_bin",
            "ema15_bin",
            "target_c",
            "trades",
            "avg_entry_c",
            "hit_rate",
            "breakeven_hit_rate",
            "avg_pnl_c",
            "min_quartile_ev_c",
            "positive_weeks",
            "weeks",
            "median_entry_minute",
        ]
        print(robust[cols].head(40).round(4).to_string(index=False))

    print("\nTop unconditioned scalp rules")
    top_plain = plain[
        (plain["target_c"].between(65, 85)) & (plain["trades"] >= 100)
    ].sort_values("avg_pnl_c", ascending=False)
    cols = [
        "rule",
        "side",
        "target_c",
        "trades",
        "avg_entry_c",
        "hit_rate",
        "breakeven_hit_rate",
        "avg_pnl_c",
        "min_quartile_ev_c",
        "positive_weeks",
        "weeks",
    ]
    print(top_plain[cols].head(25).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
