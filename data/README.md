# Retained Data

These files are kept for fresh research. They are not an endorsement of the old
market-midpoint correction pipeline.

- `kalshi_btc15_t600_training.parquet`: historical BTC15 rows at the `t-600`
  cutoff.
- `kalshi_btc15_t600_audit.json`: audit metadata for the retained dataset.
- `synthetic_demo.parquet`: legacy synthetic smoke-test data, retained only so
  nothing data-like is lost.
- `raw/kalshi_btc15/`: original market metadata and candlesticks, including
  `candles_by_ticker/` for replaying individual markets.
- `external/btc_usd_1m_coinbase.parquet`: BTC/USD minute candles for external
  price features.

All of these data files are intended to be included in Git. Preserve the folder
structure: research scripts resolve these paths relative to the repository root.
Saved predictions and backtest outputs are included in the top-level `reports/`
directory. The audit JSON records the original training dataset's date range,
row counts, and source checksums. Synthetic data is not historical market data.

Next research should join external BTC price context at the same cutoff and test
whether it beats the market midpoint out of fold.
