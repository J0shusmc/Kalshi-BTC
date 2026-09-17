#!/usr/bin/env python3
"""Research classifier for Kalshi BTC15 50-60c entries.

The target is the actual trade shape: buy one side at the ask and exit at 99c
if the post-cutoff candle path gives that opportunity; otherwise settle.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRAINING = DATA / "kalshi_btc15_t600_training.parquet"
CANDLES_DIR = DATA / "raw" / "kalshi_btc15" / "candles_by_ticker"
KALSHI_MARKETS = DATA / "raw" / "kalshi_btc15" / "live_markets.jsonl.gz"
BTC_COINBASE = DATA / "external" / "btc_usd_1m_coinbase.parquet"


def _num(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def _price(node: dict, key: str) -> float:
    return _num(node.get(f"{key}_dollars", node.get(key)))


def load_candles(ticker: str) -> list[dict]:
    path = CANDLES_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    with path.open() as fh:
        return json.load(fh).get("response", {}).get("candlesticks", [])


def load_market_meta() -> pd.DataFrame:
    rows = []
    with gzip.open(KALSHI_MARKETS, "rt") as fh:
        for line in fh:
            obj = json.loads(line)
            for market in obj.get("response", {}).get("markets", []):
                ticker = market.get("ticker")
                if not ticker:
                    continue
                rows.append(
                    {
                        "kalshi_ticker": ticker,
                        "open_time": pd.Timestamp(market["open_time"]) if market.get("open_time") else pd.NaT,
                        "close_time": pd.Timestamp(market["close_time"]) if market.get("close_time") else pd.NaT,
                        "floor_strike": _num(market.get("floor_strike")),
                        "expiration_value": _num(market.get("expiration_value")),
                    }
                )
    return pd.DataFrame(rows).drop_duplicates("kalshi_ticker")


def load_btc_features(df: pd.DataFrame) -> pd.DataFrame:
    if not BTC_COINBASE.exists():
        return pd.DataFrame({"kalshi_ticker": df["kalshi_ticker"]})

    btc = pd.read_parquet(BTC_COINBASE).sort_values("ts").reset_index(drop=True)
    btc["ts"] = pd.to_datetime(btc["ts"], utc=True).astype("datetime64[ns, UTC]")
    btc["btc_ret_1m"] = btc["close"].diff(1)
    btc["btc_ret_5m"] = btc["close"].diff(5)
    btc["btc_ret_15m"] = btc["close"].diff(15)
    btc["btc_range_5m"] = btc["high"].rolling(5, min_periods=2).max() - btc["low"].rolling(5, min_periods=2).min()
    btc["btc_range_15m"] = btc["high"].rolling(15, min_periods=5).max() - btc["low"].rolling(15, min_periods=5).min()
    btc["btc_vol_5m"] = btc["btc_ret_1m"].rolling(5, min_periods=3).std()
    btc["btc_vol_15m"] = btc["btc_ret_1m"].rolling(15, min_periods=5).std()
    btc["btc_body_5m"] = btc["close"] - btc["open"].shift(4)
    btc["btc_body_15m"] = btc["close"] - btc["open"].shift(14)
    btc["btc_upper_wick_5m"] = btc["high"].rolling(5, min_periods=2).max() - btc[["open", "close"]].rolling(5, min_periods=2).max().max(axis=1)
    btc["btc_lower_wick_5m"] = btc[["open", "close"]].rolling(5, min_periods=2).min().min(axis=1) - btc["low"].rolling(5, min_periods=2).min()
    btc["btc_upper_wick_15m"] = btc["high"].rolling(15, min_periods=5).max() - btc[["open", "close"]].rolling(15, min_periods=5).max().max(axis=1)
    btc["btc_lower_wick_15m"] = btc[["open", "close"]].rolling(15, min_periods=5).min().min(axis=1) - btc["low"].rolling(15, min_periods=5).min()
    btc["btc_volume_5m"] = btc["volume"].rolling(5, min_periods=2).sum()
    btc["btc_volume_15m"] = btc["volume"].rolling(15, min_periods=5).sum()

    left = df[["kalshi_ticker", "feature_cutoff", "open_time", "close_time", "floor_strike"]].copy()
    left["feature_cutoff"] = pd.to_datetime(left["feature_cutoff"], utc=True).astype("datetime64[ns, UTC]")
    left["open_time"] = pd.to_datetime(left["open_time"], utc=True).astype("datetime64[ns, UTC]")
    left["close_time"] = pd.to_datetime(left["close_time"], utc=True).astype("datetime64[ns, UTC]")
    left["btc_feature_time"] = left["feature_cutoff"] - pd.Timedelta(minutes=1)
    left = left.sort_values("feature_cutoff").reset_index(drop=True)
    joined = pd.merge_asof(
        left,
        btc,
        left_on="btc_feature_time",
        right_on="ts",
        direction="backward",
        tolerance=pd.Timedelta(minutes=2),
    )
    joined = joined.rename(
        columns={
            "close": "btc_close",
            "open": "btc_open_1m",
            "high": "btc_high_1m",
            "low": "btc_low_1m",
            "volume": "btc_volume_1m",
        }
    )
    joined["btc_dist_to_target"] = joined["btc_close"] - joined["floor_strike"]
    joined["btc_dist_to_target_bps"] = joined["btc_dist_to_target"] / joined["floor_strike"] * 10_000
    joined["btc_dist_to_target_vol_5m"] = joined["btc_dist_to_target"] / joined["btc_vol_5m"].replace(0, np.nan)
    joined["btc_dist_to_target_vol_15m"] = joined["btc_dist_to_target"] / joined["btc_vol_15m"].replace(0, np.nan)

    # Boundary proxy features. Kalshi compares the 60-second average before the
    # close boundary to the 60-second average before the open boundary. Coinbase
    # closes are only a proxy for that reference source, so we use them to
    # estimate target-source basis, not as a substitute settlement label.
    indexed_btc = btc.set_index("ts").sort_index()
    boundary_rows = []
    for row in joined.itertuples(index=False):
        prior_boundary = indexed_btc.loc[: row.open_time - pd.Timedelta(minutes=1)].tail(1)
        prior_boundary_close = (
            float(prior_boundary["close"].iloc[0]) if not prior_boundary.empty else np.nan
        )
        target_basis = prior_boundary_close - row.floor_strike
        boundary_rows.append(
            {
                "kalshi_ticker": row.kalshi_ticker,
                "btc_prior_boundary_close": prior_boundary_close,
                "btc_target_basis": target_basis,
                "btc_target_basis_bps": target_basis / row.floor_strike * 10_000
                if not np.isnan(target_basis)
                else np.nan,
            }
        )
    boundary = pd.DataFrame(boundary_rows)
    joined = joined.merge(boundary, on="kalshi_ticker", how="left")
    return joined.drop(columns=["feature_cutoff", "btc_feature_time", "open_time", "close_time", "floor_strike"])


def candle_path_features(row: pd.Series) -> dict[str, float | bool]:
    candles = load_candles(row["kalshi_ticker"])
    cutoff_ts = int(pd.Timestamp(row["feature_cutoff"]).timestamp())
    before = [c for c in candles if c.get("end_period_ts", 0) <= cutoff_ts]
    after = [c for c in candles if c.get("end_period_ts", 0) > cutoff_ts]

    yes_hit99 = False
    no_hit99 = False
    yes_hit90 = False
    no_hit90 = False
    yes_hit80 = False
    no_hit80 = False
    yes_minutes99 = np.nan
    no_minutes99 = np.nan
    yes_max_bid_after = np.nan
    no_max_bid_after = np.nan

    yes_bid_highs: list[float] = []
    no_bid_highs: list[float] = []
    for candle in after:
        minutes_after = (candle.get("end_period_ts", cutoff_ts) - cutoff_ts) / 60
        yes_ask = candle.get("yes_ask", {}) or {}
        yes_bid = candle.get("yes_bid", {}) or {}
        yb_high = _price(yes_bid, "high")
        ya_low = _price(yes_ask, "low")

        if not np.isnan(yb_high):
            yes_bid_highs.append(yb_high)
            yes_hit80 = yes_hit80 or yb_high >= 0.80
            yes_hit90 = yes_hit90 or yb_high >= 0.90
            if yb_high >= 0.99 and not yes_hit99:
                yes_hit99 = True
                yes_minutes99 = minutes_after

        if not np.isnan(ya_low):
            no_bid = 1.0 - ya_low
            no_bid_highs.append(no_bid)
            no_hit80 = no_hit80 or no_bid >= 0.80
            no_hit90 = no_hit90 or no_bid >= 0.90
            if no_bid >= 0.99 and not no_hit99:
                no_hit99 = True
                no_minutes99 = minutes_after

    if yes_bid_highs:
        yes_max_bid_after = max(yes_bid_highs)
    if no_bid_highs:
        no_max_bid_after = max(no_bid_highs)

    closes = []
    ranges = []
    volumes = []
    for candle in before:
        price = candle.get("price", {}) or {}
        close = _price(price, "close")
        high = _price(price, "high")
        low = _price(price, "low")
        volume = _num(candle.get("volume_fp", candle.get("volume")))
        if not np.isnan(close):
            closes.append(close)
        if not np.isnan(high) and not np.isnan(low):
            ranges.append(high - low)
        if not np.isnan(volume):
            volumes.append(volume)

    pre_close = closes[-1] if closes else np.nan
    pre_open = closes[0] if closes else np.nan
    pre_return = pre_close - pre_open if closes else np.nan
    pre_range_mean = float(np.mean(ranges)) if ranges else np.nan
    pre_volume_sum = float(np.sum(volumes)) if volumes else np.nan

    return {
        "yes_hit80": yes_hit80,
        "yes_hit90": yes_hit90,
        "yes_hit99": yes_hit99,
        "yes_minutes99": yes_minutes99,
        "yes_max_bid_after": yes_max_bid_after,
        "no_hit80": no_hit80,
        "no_hit90": no_hit90,
        "no_hit99": no_hit99,
        "no_minutes99": no_minutes99,
        "no_max_bid_after": no_max_bid_after,
        "pre_close": pre_close,
        "pre_return": pre_return,
        "pre_range_mean": pre_range_mean,
        "pre_volume_sum": pre_volume_sum,
    }


def build_side_rows() -> pd.DataFrame:
    df = pd.read_parquet(TRAINING).sort_values("market_end").reset_index(drop=True)
    df = df.merge(load_market_meta(), on="kalshi_ticker", how="left")
    btc_features = load_btc_features(df)
    path = pd.DataFrame([candle_path_features(row) for _, row in df.iterrows()])
    wide = pd.concat([df, path], axis=1).merge(btc_features, on="kalshi_ticker", how="left")

    rows = []
    for _, row in wide.iterrows():
        for side in ("YES", "NO"):
            is_yes = side == "YES"
            cost = float(row["yes_cash_cost"] if is_yes else row["no_cash_cost"])
            settle_win = int(row["kalshi_outcome_up"] if is_yes else 1 - row["kalshi_outcome_up"])
            hit99 = bool(row["yes_hit99"] if is_yes else row["no_hit99"])
            hit90 = bool(row["yes_hit90"] if is_yes else row["no_hit90"])
            hit80 = bool(row["yes_hit80"] if is_yes else row["no_hit80"])
            max_bid_after = row["yes_max_bid_after"] if is_yes else row["no_max_bid_after"]
            minutes99 = row["yes_minutes99"] if is_yes else row["no_minutes99"]

            side_sign = 1 if is_yes else -1
            market_side_prob = float(row["market_probability"] if is_yes else 1 - row["market_probability"])
            pnl_hold = 1 - cost if settle_win else -cost
            pnl_99 = 0.99 - cost if hit99 else pnl_hold

            out = {
                "kalshi_ticker": row["kalshi_ticker"],
                "market_end": row["market_end"],
                "side": side,
                "cost": cost,
                "settle_win": settle_win,
                "hit80": int(hit80),
                "hit90": int(hit90),
                "hit99": int(hit99),
                "max_bid_after": max_bid_after,
                "minutes99": minutes99,
                "pnl_hold": pnl_hold,
                "pnl_99": pnl_99,
                "market_side_prob": market_side_prob,
                "signed_market_logit": side_sign * row["market_logit"],
                "signed_midpoint_change_1m": side_sign * row["midpoint_change_1m"],
                "signed_midpoint_change_2m": side_sign * row["midpoint_change_2m"],
                "signed_midpoint_change_3m": side_sign * row["midpoint_change_3m"],
                "signed_logit_change_1m": side_sign * row["logit_change_1m"],
                "signed_logit_change_2m": side_sign * row["logit_change_2m"],
                "signed_logit_change_3m": side_sign * row["logit_change_3m"],
                "signed_logit_slope_2m": side_sign * row["logit_slope_2m"],
                "signed_logit_slope_3m": side_sign * row["logit_slope_3m"],
                "signed_logit_slope_5m": side_sign * row["logit_slope_5m"],
                "spread": row["spread"],
                "candle_range": row["candle_range"],
                "log1p_volume": row["log1p_volume"],
                "log1p_open_interest": row["log1p_open_interest"],
                "distance_from_50": row["distance_from_50"],
                "latest_trade_mid_gap_signed": side_sign * row["latest_trade_mid_gap"],
                "pre_close_side": row["pre_close"] if is_yes else 1 - row["pre_close"],
                "pre_return_signed": side_sign * row["pre_return"],
                "pre_range_mean": row["pre_range_mean"],
                "pre_volume_sum": row["pre_volume_sum"],
                "signed_btc_dist_to_target": side_sign * row.get("btc_dist_to_target", np.nan),
                "signed_btc_dist_to_target_bps": side_sign * row.get("btc_dist_to_target_bps", np.nan),
                "signed_btc_dist_to_target_vol_5m": side_sign * row.get("btc_dist_to_target_vol_5m", np.nan),
                "signed_btc_dist_to_target_vol_15m": side_sign * row.get("btc_dist_to_target_vol_15m", np.nan),
                "signed_btc_ret_5m": side_sign * row.get("btc_ret_5m", np.nan),
                "signed_btc_ret_15m": side_sign * row.get("btc_ret_15m", np.nan),
                "signed_btc_body_5m": side_sign * row.get("btc_body_5m", np.nan),
                "signed_btc_body_15m": side_sign * row.get("btc_body_15m", np.nan),
                "btc_target_basis_abs": abs(row.get("btc_target_basis", np.nan)),
                "btc_target_basis_bps_abs": abs(row.get("btc_target_basis_bps", np.nan)),
                "btc_range_5m": row.get("btc_range_5m", np.nan),
                "btc_range_15m": row.get("btc_range_15m", np.nan),
                "btc_vol_5m": row.get("btc_vol_5m", np.nan),
                "btc_vol_15m": row.get("btc_vol_15m", np.nan),
                "btc_upper_wick_5m": row.get("btc_upper_wick_5m", np.nan),
                "btc_lower_wick_5m": row.get("btc_lower_wick_5m", np.nan),
                "btc_upper_wick_15m": row.get("btc_upper_wick_15m", np.nan),
                "btc_lower_wick_15m": row.get("btc_lower_wick_15m", np.nan),
                "btc_volume_5m": row.get("btc_volume_5m", np.nan),
                "btc_volume_15m": row.get("btc_volume_15m", np.nan),
                "time_sin": row["time_sin"],
                "time_cos": row["time_cos"],
                "is_weekend": row["is_weekend"],
            }
            rows.append(out)

    return pd.DataFrame(rows).sort_values(["market_end", "kalshi_ticker", "side"]).reset_index(drop=True)


FEATURE_COLS = [
        "cost",
        "market_side_prob",
        "signed_market_logit",
        "signed_midpoint_change_1m",
        "signed_midpoint_change_2m",
        "signed_midpoint_change_3m",
        "signed_logit_change_1m",
        "signed_logit_change_2m",
        "signed_logit_change_3m",
        "signed_logit_slope_2m",
        "signed_logit_slope_3m",
        "signed_logit_slope_5m",
        "spread",
        "candle_range",
        "log1p_volume",
        "log1p_open_interest",
        "distance_from_50",
        "latest_trade_mid_gap_signed",
        "pre_close_side",
        "pre_return_signed",
        "pre_range_mean",
        "pre_volume_sum",
        "signed_btc_dist_to_target",
        "signed_btc_dist_to_target_bps",
        "signed_btc_dist_to_target_vol_5m",
        "signed_btc_dist_to_target_vol_15m",
        "signed_btc_ret_5m",
        "signed_btc_ret_15m",
        "signed_btc_body_5m",
        "signed_btc_body_15m",
        "btc_target_basis_abs",
        "btc_target_basis_bps_abs",
        "btc_range_5m",
        "btc_range_15m",
        "btc_vol_5m",
        "btc_vol_15m",
        "btc_upper_wick_5m",
        "btc_lower_wick_5m",
        "btc_upper_wick_15m",
        "btc_lower_wick_15m",
        "btc_volume_5m",
        "btc_volume_15m",
        "time_sin",
        "time_cos",
        "is_weekend",
]


def chronological_predictions(side_rows: pd.DataFrame, target: str, prefix: str = "") -> pd.DataFrame:
    feature_cols = FEATURE_COLS

    data = side_rows.copy()
    n = len(data)
    cuts = np.linspace(int(n * 0.25), n, 6, dtype=int)
    folds = [(np.arange(0, start), np.arange(start, stop)) for start, stop in zip(cuts[:-1], cuts[1:])]

    models = {
        "logit": make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(max_iter=3000, C=0.5),
        ),
        "hgb": HistGradientBoostingClassifier(
            max_iter=160,
            learning_rate=0.025,
            max_leaf_nodes=10,
            l2_regularization=0.5,
            random_state=7,
        ),
    }

    y = data[target].astype(int).to_numpy()
    for name, model in models.items():
        pred = np.full(n, np.nan)
        for train_idx, test_idx in folds:
            model.fit(data.loc[train_idx, feature_cols], y[train_idx])
            pred[test_idx] = model.predict_proba(data.loc[test_idx, feature_cols])[:, 1]
        data[f"pred_{prefix}{target}_{name}"] = pred

    if not prefix:
        data[f"baseline_{target}"] = data["market_side_prob"]
    return data


def metric_line(df: pd.DataFrame, pred_col: str, target: str) -> dict[str, float]:
    mask = df[pred_col].notna()
    y = df.loc[mask, target].astype(int)
    pred = df.loc[mask, pred_col].astype(float).clip(0.001, 0.999)
    return {
        "n": int(mask.sum()),
        "brier": brier_score_loss(y, pred),
        "logloss": log_loss(y, pred),
        "auc": roc_auc_score(y, pred),
    }


def select_one_trade_per_market(candidates: pd.DataFrame, pred_col: str, threshold: float) -> pd.DataFrame:
    filt = candidates[candidates[pred_col] >= threshold].copy()
    if filt.empty:
        return filt
    filt["pred_edge"] = filt[pred_col] - filt["cost"]
    filt = filt.sort_values(["market_end", "kalshi_ticker", "pred_edge"], ascending=[True, True, False])
    return filt.groupby("kalshi_ticker", as_index=False).head(1)


def summarize_strategy(df: pd.DataFrame, pred_col: str) -> pd.DataFrame:
    base = df[(df["cost"] >= 0.50) & (df["cost"] < 0.60)].copy()
    rows = []
    for threshold in np.arange(0.54, 0.70, 0.01):
        selected = select_one_trade_per_market(base, pred_col, float(threshold))
        if selected.empty:
            continue
        rows.append(
            {
                "pred_col": pred_col,
                "threshold": round(float(threshold), 2),
                "trades": len(selected),
                "avg_cost": selected["cost"].mean(),
                "hit99": selected["hit99"].mean(),
                "settle_win": selected["settle_win"].mean(),
                "avg_pnl_99_c": selected["pnl_99"].mean() * 100,
                "avg_pnl_hold_c": selected["pnl_hold"].mean() * 100,
                "median_minutes99": selected["minutes99"].dropna().median(),
                "yes_share": (selected["side"] == "YES").mean(),
            }
        )
    return pd.DataFrame(rows)


def focus_trained_predictions(modeled: pd.DataFrame, target: str) -> pd.DataFrame:
    focus_mask = (modeled["cost"] >= 0.50) & (modeled["cost"] < 0.60)
    focus = chronological_predictions(modeled.loc[focus_mask].reset_index(drop=False), target, prefix="focus_")
    pred_cols = [c for c in focus.columns if c.startswith(f"pred_focus_{target}_")]
    out = modeled.copy()
    for col in pred_cols:
        out[col] = np.nan
        out.loc[focus["index"], col] = focus[col].to_numpy()
    return out


def print_table(title: str, df: pd.DataFrame) -> None:
    print(f"\n{title}")
    if df.empty:
        print("(empty)")
    else:
        print(df.round(4).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "reports"))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    side_rows = build_side_rows()
    side_rows_path = output_dir / "btc15_side_rows.parquet"
    side_rows.to_parquet(side_rows_path, index=False)

    modeled = chronological_predictions(side_rows, "hit99")
    modeled = focus_trained_predictions(modeled, "hit99")
    modeled_path = output_dir / "btc15_hit99_predictions.parquet"
    modeled.to_parquet(modeled_path, index=False)

    focus = modeled[(modeled["cost"] >= 0.50) & (modeled["cost"] < 0.60)].copy()
    print(f"side rows: {len(modeled):,}")
    print(f"50-60c side rows: {len(focus):,}")
    print(f"wrote: {side_rows_path.relative_to(ROOT)}")
    print(f"wrote: {modeled_path.relative_to(ROOT)}")

    metrics = []
    for pred_col in [
        "baseline_hit99",
        "pred_hit99_logit",
        "pred_hit99_hgb",
        "pred_focus_hit99_logit",
        "pred_focus_hit99_hgb",
    ]:
        metrics.append({"pred_col": pred_col, **metric_line(focus, pred_col, "hit99")})
    print_table("50-60c hit99 model metrics", pd.DataFrame(metrics))

    raw = (
        focus.groupby("side", observed=True)
        .agg(
            rows=("hit99", "size"),
            avg_cost=("cost", "mean"),
            hit99=("hit99", "mean"),
            settle_win=("settle_win", "mean"),
            avg_pnl_99_c=("pnl_99", lambda x: x.mean() * 100),
            avg_pnl_hold_c=("pnl_hold", lambda x: x.mean() * 100),
            median_minutes99=("minutes99", "median"),
        )
        .reset_index()
    )
    print_table("Raw 50-60c bucket", raw)

    summaries = []
    for pred_col in [
        "pred_hit99_logit",
        "pred_hit99_hgb",
        "pred_focus_hit99_logit",
        "pred_focus_hit99_hgb",
    ]:
        summaries.append(summarize_strategy(modeled, pred_col))
    strategy = pd.concat(summaries, ignore_index=True)
    strategy_path = output_dir / "btc15_50_60_strategy_summary.csv"
    strategy.to_csv(strategy_path, index=False)
    print_table("One-trade-per-market 50-60c strategy thresholds", strategy)
    print(f"\nwrote: {strategy_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
