# BTC15 First-5 BTC Recovery Research

Generated from:

- `reports/btc15_minute_candidates.parquet`
- `data/external/btc_usd_1m_coinbase.parquet`

The test waits through minutes `1-5` of each 15-minute BTC contract, then looks
for the first recovery entry from minutes `6-10`.

## Trade Shape

- Entry: first side ask close after minute 5 that reclaims into a configured
  band, with a green 1-minute side candle.
- One trade per market-side.
- Exit: later bid high touches target; otherwise full entry loss.
- Targets tested: `60c`, `70c`, `80c`, `85c`, `90c`, `99c`.

## First Pass Finding

Broad recovery rules were not robust enough by themselves. They often showed
small positive average EV at `85c-99c`, but failed chronological quartile or
weekly stability checks.

That means the edge is probably not just:

```text
early low <= 25c
later close 45-65c
green 1m candle
```

The useful filter is BTC context from the first five minutes.

## Best Conditioned Pattern

The strongest slice found:

```text
Side: YES
First 5 minutes:
  side ask low between 25-30c
  BTC moved against YES by $25-$75
Entry:
  minute 6-10
  YES ask close 45-65c
  close > minute-5 ask close
  green 1m side candle
Target:
  90c or 99c
```

Results:

| Target | Trades | Avg Entry | Hit Rate | Breakeven | Avg EV | Min Quartile EV | Positive Weeks |
|---:|---:|---:|---:|---:|---:|---:|---:|
| `99c` | `58` | `52.1c` | `65.5%` | `52.7%` | `+12.72c` | `+9.57c` | `7/9` |
| `90c` | `58` | `52.1c` | `67.2%` | `57.9%` | `+8.38c` | `+3.79c` | `7/9` |

A slightly higher entry band also held:

```text
YES, early low 25-30c, BTC against YES by $25-$75,
entry close 50-70c, target 99c
```

Result:

- Trades: `51`
- Avg entry: `57.1c`
- Hit `99c`: `70.6%`
- Avg EV: `+12.76c`
- Min quartile EV: `+2.50c`
- Positive weeks: `7/9`

## Interpretation

The clean pattern is not just a low ask. It is:

1. The contract gets beaten down in the first five minutes.
2. BTC spot moves against that side, which explains the pressure.
3. The side refuses to die and reclaims into the middle of the book.
4. That reclaim often continues enough to reach `90c-99c`.

This matches the live example: wait early, let the panic happen, then buy the
first reclaim rather than buying the dead low.

## Output Files

- `reports/btc15_first5_btc_recovery_entries.parquet`
- `reports/btc15_first5_btc_recovery_summary.csv`
- `reports/btc15_first5_btc_recovery_btc_slices.csv`
- `reports/btc15_first5_btc_recovery_conditioned_summary.csv`

## Next Implementation Candidate

Add a BTC-aware recovery lane:

```text
YES_BTC_FADE_99
minute 6-10
early YES ask low 25-30c
BTC first-5 signed move between -75 and -25 dollars
YES ask close 45-65c
YES ask close > minute-5 YES ask close
green 1m side candle
target 90c or 99c
```

This should be tracked separately from the broad `YES_GRIND_85` monitor signal.
