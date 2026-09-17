# BTC15 EMA Decision Research

Generated from:

- `reports/btc15_minute_candidates.parquet`
- `data/external/btc_usd_1m_coinbase.parquet`

This pass tests the idea of waiting through the first `5` or `6` minutes, then
using granular Kalshi bid/ask behavior plus BTC chart context to decide whether
to play a recovered side.

## BTC Context Added

The script computes:

- BTC 1-minute EMA21.
- BTC 15-minute EMA21 from resampled Coinbase 1-minute candles.
- BTC first-window return for minutes `1-5` or `1-6`.
- Side-signed BTC return:
  - Positive supports `YES`.
  - Negative supports `NO`.
- Side-signed distance from BTC close to 15-minute EMA21:
  - Positive means BTC is on the favorable side of EMA for that Kalshi side.
  - Negative means BTC is on the unfavorable side.

## Trade Shape

- Decision windows tested: `m1-5`, `m1-6`.
- Entry window: first qualifying minute after the decision window through
  minute `10`.
- Entry condition: side ask recovered from below into a close band with a green
  1-minute side candle.
- Exit targets tested: `65c`, `70c`, `75c`, `80c`, `85c`.
- If target never appears later in the contract, full entry loss is charged.

## Best Broad Scalp

The cleanest broad rule is YES after a first-5 beatdown:

```text
m5_low15-25_close35-55_green
Side: YES
Decision: wait through minute 5
First five minutes: YES ask low >15c and <=25c
Entry: first minute 6-10 where YES ask closes 35-55c
Entry candle: green 1-minute YES candle
Target: 85c
```

Result:

- Trades: `391`
- Average entry: `43.6c`
- Hit `85c`: `55.5%`
- Breakeven hit rate: `51.3%`
- Average EV: `+3.60c`
- Minimum chronological quartile EV: `+0.29c`
- Positive weeks: `8/10`

This is the best evidence so far that there is a scalable version of the
first-few-minutes wait/recovery scalp.

## Best BTC+EMA Conditioned Scalp

The strongest filtered rule:

```text
m5_low20-30_close35-55_green
Side: YES
BTC first-5 signed return: flat
BTC vs 15m EMA21: unfavorable for YES by $50-$150
Target: 70c
```

Result:

- Trades: `40`
- Average entry: `45.0c`
- Hit `70c`: `82.5%`
- Breakeven hit rate: `64.3%`
- Average EV: `+12.73c`
- Minimum chronological quartile EV: `+8.90c`
- Positive weeks: `8/10`

This is a stronger edge but smaller sample. It suggests that a contract-side
reclaim while BTC is still not fully supportive can be a mispricing/lag setup.

## Opposite Side

NO has candidate pockets, but they are less clean.

Best broad NO scalp:

```text
m6_low15-25_close50-70_green
Side: NO
Target: 80c
```

Result:

- Trades: `301`
- Average entry: `58.6c`
- Hit `80c`: `76.4%`
- Breakeven hit rate: `73.3%`
- Average EV: `+2.48c`
- Minimum chronological quartile EV: `+1.34c`
- Positive weeks: `9/10`

That is tradeable-looking, but the YES side still has the cleaner early
beatdown/recovery profile.

## Working Interpretation

The edge is not simply "buy whatever side got cheap." The better shape is:

1. Wait through the first five minutes.
2. Let one side get beaten down into the `15-25c` or `20-30c` zone.
3. Do not buy the low.
4. Buy the first green recovery candle that closes back into `35-55c` or
   `50-70c`.
5. Scalp into `70c-85c`, depending on rule strength.

For more trade frequency, the best starting monitor-only lanes are:

```text
YES_M5_SCALP_85
  first 5m YES ask low 15-25c
  minute 6-10
  YES ask close 35-55c
  green 1m YES candle
  target 85c

NO_M6_SCALP_80
  first 6m NO ask low 15-25c
  minute 7-10
  NO ask close 50-70c
  green 1m NO candle
  target 80c

YES_M5_EMA_SCALP_70
  first 5m YES ask low 20-30c
  minute 6-10
  YES ask close 35-55c
  green 1m YES candle
  BTC first-5 signed return flat
  BTC close is $50-$150 below side-favorable 15m EMA21
  target 70c
```

## Output Files

- `reports/btc15_ema_decision_entries.parquet`
- `reports/btc15_ema_decision_summary.csv`
- `reports/btc15_ema_decision_btc_ema_summary.csv`
- `reports/btc15_ema_decision_btc_ema_slope_summary.csv`
