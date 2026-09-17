#!/usr/bin/env python3
"""Predict BTC15 settlement from the first five completed market minutes.

The research is deliberately walk-forward. Models only train on older markets,
the final fold is held out from model/threshold selection, and trade scoring
uses the observed minute-five ask plus a configurable fee/slippage allowance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

try:
    from scripts.btc_classifier_research import load_market_meta
except ModuleNotFoundError:
    from btc_classifier_research import load_market_meta


ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "data" / "kalshi_btc15_t600_training.parquet"
CANDLES_DIR = ROOT / "data" / "raw" / "kalshi_btc15" / "candles_by_ticker"
BTC = ROOT / "data" / "external" / "btc_usd_1m_coinbase.parquet"
REPORTS = ROOT / "reports"
SIDE_ROWS = REPORTS / "btc15_side_rows.parquet"

META_COLUMNS = {
    "kalshi_ticker",
    "market_start",
    "market_end",
    "feature_cutoff",
    "decision_time",
    "kalshi_outcome_up",
}


def number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def node_price(candle: dict, node: str, field: str) -> float:
    payload = candle.get(node, {}) or {}
    return number(payload.get(f"{field}_dollars", payload.get(field)))


def kalshi_sequence_features(row: pd.Series) -> dict[str, float]:
    path = CANDLES_DIR / f"{row['kalshi_ticker']}.json"
    if not path.exists():
        return {}
    candles = json.loads(path.read_text()).get("response", {}).get("candlesticks", [])
    cutoff = int(pd.Timestamp(row["feature_cutoff"]).timestamp())
    candles = sorted(
        (c for c in candles if int(c.get("end_period_ts") or 0) <= cutoff),
        key=lambda c: int(c.get("end_period_ts") or 0),
    )[-5:]

    out: dict[str, float] = {}
    for minute, candle in enumerate(candles, start=1):
        for node in ("yes_bid", "yes_ask", "price"):
            for field in ("open", "high", "low", "close"):
                out[f"m{minute}_{node}_{field}"] = node_price(candle, node, field)
        bid_close = out[f"m{minute}_yes_bid_close"]
        ask_close = out[f"m{minute}_yes_ask_close"]
        out[f"m{minute}_spread"] = ask_close - bid_close
        out[f"m{minute}_volume_log"] = np.log1p(
            max(0.0, number(candle.get("volume_fp", candle.get("volume"))))
        )
        out[f"m{minute}_oi_log"] = np.log1p(
            max(0.0, number(candle.get("open_interest_fp", candle.get("open_interest"))))
        )
    return out


def add_btc_sequence_features(frame: pd.DataFrame) -> pd.DataFrame:
    btc = pd.read_parquet(BTC).copy()
    btc["ts"] = pd.to_datetime(btc["ts"], utc=True).astype("datetime64[ns, UTC]")
    btc = btc.set_index("ts").sort_index()
    output = frame.copy()

    for minute in range(1, 6):
        timestamps = output["market_start"] + pd.to_timedelta(minute - 1, unit="m")
        aligned = btc.reindex(pd.DatetimeIndex(timestamps)).reset_index(drop=True)
        open_price = aligned["open"].to_numpy(dtype=float)
        close_price = aligned["close"].to_numpy(dtype=float)
        strike = output["floor_strike"].to_numpy(dtype=float)
        output[f"btc_m{minute}_return_bps"] = (close_price / open_price - 1) * 10_000
        output[f"btc_m{minute}_range_bps"] = (
            (aligned["high"].to_numpy(dtype=float) - aligned["low"].to_numpy(dtype=float))
            / open_price
            * 10_000
        )
        output[f"btc_m{minute}_strike_distance_bps"] = (close_price - strike) / strike * 10_000
        output[f"btc_m{minute}_volume_log"] = np.log1p(aligned["volume"].to_numpy(dtype=float))

    output["btc_first5_return_bps"] = (
        (output["btc_m5_strike_distance_bps"] - output["btc_m1_strike_distance_bps"])
    )
    output["btc_kalshi_divergence"] = (
        output["btc_m5_strike_distance_bps"] - output["market_logit"] * 10
    )
    return output


def build_features() -> pd.DataFrame:
    frame = pd.read_parquet(TRAINING).sort_values("market_end").reset_index(drop=True)
    meta = load_market_meta()[["kalshi_ticker", "floor_strike"]]
    frame = frame.merge(meta, on="kalshi_ticker", how="left")
    sequence = pd.DataFrame(
        [kalshi_sequence_features(row) for _, row in frame.iterrows()], index=frame.index
    )
    frame = pd.concat([frame, sequence], axis=1)
    return add_btc_sequence_features(frame)


def model_factories() -> dict[str, object]:
    return {
        "logit": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(C=0.12, max_iter=3000),
        ),
        "hist_gb": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            HistGradientBoostingClassifier(
                max_iter=220,
                learning_rate=0.03,
                max_leaf_nodes=12,
                min_samples_leaf=40,
                l2_regularization=3,
                random_state=7,
            ),
        ),
        "lightgbm": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            LGBMClassifier(
                n_estimators=300,
                learning_rate=0.02,
                num_leaves=12,
                max_depth=5,
                min_child_samples=45,
                reg_alpha=0.75,
                reg_lambda=4,
                verbosity=-1,
                random_state=7,
            ),
        ),
        "xgboost": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            XGBClassifier(
                n_estimators=300,
                learning_rate=0.02,
                max_depth=3,
                min_child_weight=25,
                subsample=0.8,
                colsample_bytree=0.75,
                reg_alpha=0.75,
                reg_lambda=5,
                n_jobs=4,
                eval_metric="logloss",
                random_state=7,
            ),
        ),
    }


def calibrate(raw_calibration: np.ndarray, y_calibration: np.ndarray, raw_test: np.ndarray) -> np.ndarray:
    eps = 1e-5
    calibration_logit = np.log(
        np.clip(raw_calibration, eps, 1 - eps) / np.clip(1 - raw_calibration, eps, 1 - eps)
    ).reshape(-1, 1)
    test_logit = np.log(
        np.clip(raw_test, eps, 1 - eps) / np.clip(1 - raw_test, eps, 1 - eps)
    ).reshape(-1, 1)
    mapper = LogisticRegression(C=1.0).fit(calibration_logit, y_calibration)
    return mapper.predict_proba(test_logit)[:, 1]


def tower_model() -> object:
    return make_pipeline(
        SimpleImputer(strategy="median"),
        LGBMClassifier(
            n_estimators=280,
            learning_rate=0.02,
            num_leaves=10,
            max_depth=4,
            min_child_samples=50,
            reg_alpha=1,
            reg_lambda=5,
            verbosity=-1,
            random_state=17,
        ),
    )


def probability_logit(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, 1e-5, 1 - 1e-5)
    return np.log(clipped / (1 - clipped))


def stack_features(market: np.ndarray, btc: np.ndarray, kalshi: np.ndarray) -> np.ndarray:
    return np.column_stack(
        [
            probability_logit(market),
            probability_logit(btc),
            probability_logit(kalshi),
            btc - market,
            kalshi - market,
            btc - kalshi,
        ]
    )


def walk_forward_predictions(frame: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    output = frame.copy()
    x = frame[features]
    btc_features = [
        column
        for column in features
        if column.startswith("btc_") and column != "btc_kalshi_divergence"
    ]
    kalshi_features = [column for column in features if not column.startswith("btc_")]
    x_btc = frame[btc_features]
    x_kalshi = frame[kalshi_features]
    y = frame["kalshi_outcome_up"].astype(int).to_numpy()
    n = len(frame)
    boundaries = np.linspace(int(n * 0.40), n, 5, dtype=int)
    folds = list(zip(boundaries[:-1], boundaries[1:]))
    fold_ids = np.full(n, -1, dtype=int)
    factories = model_factories()
    predictions = {name: np.full(n, np.nan) for name in factories}
    residual_predictions = np.full(n, np.nan)
    btc_predictions = np.full(n, np.nan)
    kalshi_predictions = np.full(n, np.nan)
    stack_predictions = np.full(n, np.nan)

    for fold_id, (start, stop) in enumerate(folds):
        fold_ids[start:stop] = fold_id
        calibration_start = max(500, int(start * 0.82))
        for name, factory in factories.items():
            calibration_model = factory()
            calibration_model.fit(x.iloc[:calibration_start], y[:calibration_start])
            raw_calibration = calibration_model.predict_proba(x.iloc[calibration_start:start])[:, 1]
            final_model = factory()
            final_model.fit(x.iloc[:start], y[:start])
            raw_test = final_model.predict_proba(x.iloc[start:stop])[:, 1]
            predictions[name][start:stop] = calibrate(
                raw_calibration, y[calibration_start:start], raw_test
            )

        btc_calibration_model = tower_model()
        kalshi_calibration_model = tower_model()
        btc_calibration_model.fit(x_btc.iloc[:calibration_start], y[:calibration_start])
        kalshi_calibration_model.fit(x_kalshi.iloc[:calibration_start], y[:calibration_start])
        btc_calibration = btc_calibration_model.predict_proba(
            x_btc.iloc[calibration_start:start]
        )[:, 1]
        kalshi_calibration = kalshi_calibration_model.predict_proba(
            x_kalshi.iloc[calibration_start:start]
        )[:, 1]
        market_calibration = frame.loc[
            calibration_start : start - 1, "market_probability"
        ].to_numpy(dtype=float)
        stack_model = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=0.15, max_iter=2000),
        )
        stack_model.fit(
            stack_features(market_calibration, btc_calibration, kalshi_calibration),
            y[calibration_start:start],
        )

        btc_final_model = tower_model()
        kalshi_final_model = tower_model()
        btc_final_model.fit(x_btc.iloc[:start], y[:start])
        kalshi_final_model.fit(x_kalshi.iloc[:start], y[:start])
        btc_test = btc_final_model.predict_proba(x_btc.iloc[start:stop])[:, 1]
        kalshi_test = kalshi_final_model.predict_proba(x_kalshi.iloc[start:stop])[:, 1]
        market_test = frame.loc[start : stop - 1, "market_probability"].to_numpy(dtype=float)
        btc_predictions[start:stop] = calibrate(
            btc_calibration, y[calibration_start:start], btc_test
        )
        kalshi_predictions[start:stop] = calibrate(
            kalshi_calibration, y[calibration_start:start], kalshi_test
        )
        stack_predictions[start:stop] = stack_model.predict_proba(
            stack_features(market_test, btc_test, kalshi_test)
        )[:, 1]

        residual_model = make_pipeline(
            SimpleImputer(strategy="median"),
            LGBMRegressor(
                n_estimators=250,
                learning_rate=0.02,
                num_leaves=10,
                max_depth=4,
                min_child_samples=50,
                reg_alpha=1,
                reg_lambda=5,
                verbosity=-1,
                random_state=7,
            ),
        )
        residual = y[:start] - frame.loc[: start - 1, "market_probability"].to_numpy()
        residual_model.fit(x.iloc[:start], residual)
        residual_predictions[start:stop] = np.clip(
            frame.loc[start : stop - 1, "market_probability"].to_numpy()
            + residual_model.predict(x.iloc[start:stop]),
            0.005,
            0.995,
        )

    output["fold"] = fold_ids
    output["p_market"] = output["market_probability"]
    for name, values in predictions.items():
        output[f"p_{name}"] = values
    output["p_residual_lightgbm"] = residual_predictions
    output["p_btc_signal"] = btc_predictions
    output["p_kalshi_signal"] = kalshi_predictions
    output["p_dual_stack"] = stack_predictions
    model_columns = [f"p_{name}" for name in factories]
    output["p_ensemble"] = output[model_columns].mean(axis=1)
    output["p_market_blend"] = 0.65 * output["p_market"] + 0.35 * output["p_ensemble"]
    return output, fold_ids


def model_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    mask = predictions["fold"] >= 0
    y = predictions.loc[mask, "kalshi_outcome_up"].astype(int)
    rows = []
    for column in [c for c in predictions if c.startswith("p_")]:
        probability = predictions.loc[mask, column]
        if probability.isna().any():
            continue
        rows.append(
            {
                "model": column,
                "auc": roc_auc_score(y, probability),
                "brier": brier_score_loss(y, probability),
                "log_loss": log_loss(y, probability),
            }
        )
    return pd.DataFrame(rows).sort_values("brier")


def add_exit_labels(frame: pd.DataFrame) -> pd.DataFrame:
    sides = pd.read_parquet(SIDE_ROWS)[
        ["kalshi_ticker", "side", "hit80", "hit90", "hit99"]
    ].drop_duplicates(["kalshi_ticker", "side"])
    wide = sides.pivot(index="kalshi_ticker", columns="side", values=["hit80", "hit90", "hit99"])
    wide.columns = [f"{side.lower()}_{label}" for label, side in wide.columns]
    return frame.merge(wide.reset_index(), on="kalshi_ticker", how="left")


def score_trades(
    frame: pd.DataFrame,
    probability_column: str,
    edge: float,
    cost_low: float,
    cost_high: float,
    fee_c: float,
    target_c: int,
) -> dict:
    p_up = frame[probability_column].to_numpy(dtype=float)
    yes_cost = frame["yes_cash_cost"].to_numpy(dtype=float)
    no_cost = frame["no_cash_cost"].to_numpy(dtype=float)
    yes_edge = p_up - yes_cost
    no_edge = (1 - p_up) - no_cost
    buy_yes = yes_edge >= no_edge
    best_edge = np.maximum(yes_edge, no_edge)
    cost = np.where(buy_yes, yes_cost, no_cost)
    selected = (best_edge >= edge) & (cost >= cost_low) & (cost < cost_high)
    winner = np.where(
        buy_yes,
        frame["kalshi_outcome_up"].to_numpy(dtype=int) == 1,
        frame["kalshi_outcome_up"].to_numpy(dtype=int) == 0,
    )
    settlement_pnl = np.where(winner, 100 - cost * 100 - fee_c, -cost * 100 - fee_c)
    if target_c < 100:
        yes_hit = frame[f"yes_hit{target_c}"].fillna(0).to_numpy(dtype=bool)
        no_hit = frame[f"no_hit{target_c}"].fillna(0).to_numpy(dtype=bool)
        target_hit = np.where(buy_yes, yes_hit, no_hit)
        pnl = np.where(target_hit, target_c - cost * 100 - fee_c, settlement_pnl)
    else:
        pnl = settlement_pnl
    pnl_c = pnl[selected]
    gross_profit = pnl_c[pnl_c > 0].sum()
    gross_loss = -pnl_c[pnl_c < 0].sum()
    return {
        "trades": len(pnl_c),
        "avg_pnl_c": pnl_c.mean() if len(pnl_c) else np.nan,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else np.nan,
        "win_rate": (pnl_c > 0).mean() if len(pnl_c) else np.nan,
        "yes_share": buy_yes[selected].mean() if selected.any() else np.nan,
    }


def strategy_search(predictions: pd.DataFrame, fee_c: float) -> pd.DataFrame:
    discovery = predictions[predictions["fold"].between(0, 2)]
    validation = predictions[predictions["fold"] == 3]
    rows = []
    probability_columns = [c for c in predictions if c.startswith("p_")]
    for probability_column in probability_columns:
        if predictions[probability_column].isna().all():
            continue
        for edge in np.arange(0.02, 0.201, 0.01):
            for cost_low, cost_high in ((0, 1), (0.30, 0.60), (0.40, 0.70), (0.50, 0.80), (0.60, 0.90)):
                for target_c in (80, 90, 99, 100):
                    train = score_trades(
                        discovery, probability_column, edge, cost_low, cost_high, fee_c, target_c
                    )
                    if train["trades"] < 100:
                        continue
                    test = score_trades(
                        validation, probability_column, edge, cost_low, cost_high, fee_c, target_c
                    )
                    rows.append(
                        {
                            "model": probability_column,
                            "edge": edge,
                            "cost_low": cost_low,
                            "cost_high": cost_high,
                            "target_c": target_c,
                            **{f"discovery_{key}": value for key, value in train.items()},
                            **{f"validation_{key}": value for key, value in test.items()},
                        }
                    )
    return pd.DataFrame(rows).sort_values(
        ["discovery_profit_factor", "discovery_trades"], ascending=False
    )


def write_report(metrics: pd.DataFrame, strategies: pd.DataFrame, fee_c: float) -> None:
    robust = strategies[
        (strategies["discovery_profit_factor"] > 1)
        & (strategies["validation_profit_factor"] > 1)
        & (strategies["validation_trades"] >= 25)
    ].sort_values(["validation_profit_factor", "validation_trades"], ascending=False)
    pf5 = robust[
        (robust["discovery_profit_factor"] >= 5)
        & (robust["validation_profit_factor"] >= 5)
    ]
    lines = [
        "# BTC15 First-Five Outcome Model",
        "",
        "Leakage-safe expanding-window research using only information available after five completed minutes.",
        "",
        f"- Fee/slippage allowance: `{fee_c:.1f}c` per contract",
        "- Threshold selection: folds 0-2",
        "- Untouched validation: fold 3",
        f"- Strategies with PF >= 5 in both periods: `{len(pf5)}`",
        "",
        "## Model Metrics",
        "",
        "```text",
        metrics.round(4).to_string(index=False),
        "```",
        "",
        "## Best Repeated Trade Rules",
        "",
        "```text",
        robust.head(20).round(4).to_string(index=False) if not robust.empty else "No rule was profitable in both discovery and validation.",
        "```",
        "",
        "PF 5 is a research target, not a reason to accept a small or unstable sample.",
    ]
    (REPORTS / "btc15_first5_outcome_research.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fee-c", type=float, default=6.5)
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)

    features = build_features()
    feature_columns = [
        column
        for column in features
        if column not in META_COLUMNS
        and column != "floor_strike"
        and pd.api.types.is_numeric_dtype(features[column])
    ]
    predictions, _ = walk_forward_predictions(features, feature_columns)
    predictions = add_exit_labels(predictions)
    metrics = model_metrics(predictions)
    strategies = strategy_search(predictions, args.fee_c)

    features.to_parquet(REPORTS / "btc15_first5_outcome_features.parquet", index=False)
    prediction_columns = list(META_COLUMNS & set(predictions.columns)) + [
        "yes_cash_cost",
        "no_cash_cost",
        "fold",
        "yes_hit80",
        "yes_hit90",
        "yes_hit99",
        "no_hit80",
        "no_hit90",
        "no_hit99",
        *[column for column in predictions if column.startswith("p_")],
    ]
    predictions[prediction_columns].to_parquet(
        REPORTS / "btc15_first5_outcome_predictions.parquet", index=False
    )
    metrics.to_csv(REPORTS / "btc15_first5_outcome_model_metrics.csv", index=False)
    strategies.to_csv(REPORTS / "btc15_first5_outcome_strategy_search.csv", index=False)
    write_report(metrics, strategies, args.fee_c)

    print(metrics.round(4).to_string(index=False))
    print("\nTop strategy candidates")
    print(strategies.head(20).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
