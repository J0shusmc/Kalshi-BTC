# BTC15 Classifier Research

Generated from `scripts/btc_classifier_research.py` using local `KXBTC15M`
data through `2026-08-20 23:00 UTC`, plus Coinbase BTC-USD candles fetched by
`scripts/fetch_btc_coinbase.py`.

## Trade Shape

- Entry universe: buy YES or NO at `50-60c`.
- Exit rule: sell at `99c` if the post-cutoff candle path touches 99; otherwise
  settle.
- Selection rule: at most one side per market.

## Current Result

Raw `50-60c` bucket:

- Rows: `1,695`
- Average cost: `54.4c`
- Hit 99: `55.8%`
- Exit-at-99 EV: `+0.82c`
- Median time to 99 after cutoff: `9 minutes`

Side split:

- NO: `56.4%` hit 99, `+1.43c` exit-at-99 EV.
- YES: `55.1%` hit 99, `+0.23c` exit-at-99 EV.

## BTC Feature Pass

Targets were trained walk-forward against `hit99`, not just final settlement.
The model columns in `reports/btc15_hit99_predictions.parquet` are:

- `pred_hit99_logit`
- `pred_hit99_hgb`
- `pred_focus_hit99_logit`
- `pred_focus_hit99_hgb`

Coinbase data is stored at 1 minute resolution, but the model uses only fully
closed candles available at decision time and exposes coarser 5m/15m features:

- Signed distance from BTC spot to the Kalshi target.
- Distance-to-target scaled by 5m/15m volatility.
- Signed 5m and 15m BTC returns.
- Signed 5m and 15m BTC body.
- 5m and 15m range, volatility, wick, and volume context.
- Coinbase-vs-Kalshi target basis from the last fully closed candle before the
  contract open boundary.

After preventing current-candle leakage, this did **not** materially improve the
walk-forward 50-60c `hit99` classifier:

- Baseline market-side probability AUC: `0.532`
- Kalshi + BTC logistic AUC: `0.528`
- Kalshi + BTC HGB AUC: `0.517`

The best broad, non-tiny threshold from this pass was the all-row logistic model:

- Threshold `0.60`
- Trades: `140`
- Average cost: `57.3c`
- Hit 99: `65.7%`
- Exit-at-99 EV: `+7.73c`

Other usable thresholds were less selective:

- Logistic threshold `0.58`: `268` trades, `60.8%` hit 99, `+3.08c`.
- HGB threshold `0.63`: `49` trades, `63.3%` hit 99, `+8.76c`.
- Focus-trained HGB threshold `0.65`: `225` trades, `60.9%` hit 99, `+5.60c`.

This is worth continuing, but not yet strong enough to treat as a finished
edge. The clean result says simple Coinbase 5m/15m candles are not enough by
themselves; we need either actual BRTI-aligned price data or sharper
handcrafted features around target distance and continuation.

## Next Checks

- Add fee/slippage assumptions to the EV table.
- Break results out by week to catch drift.
- Add a no-trade zone around extreme spreads or stale Coinbase/Kalshi mismatch.
- Build a live scanner using the same feature columns once the rule is stable.

The current edge appears to come from BTC distance-to-target and 5m/15m
continuation context, not from sub-10c lottery entries.
