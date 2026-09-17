# BTC15 Wait-for-Cheaper Research

Generated from `scripts/btc15_wait_for_cheaper_research.py` using local
`KXBTC15M` candle data from `2026-06-15 00:00 UTC` through
`2026-08-20 23:00 UTC`.

## Trade Shape Tested

- Market: 15-minute BTC up/down.
- Entry: buy YES or NO only after that side's ask closes in the `50-60c` band.
- Exit: sell at `99c` if a later candle's bid touches `99c`; otherwise settle.
- Same-candle entry/exit is ignored to avoid relying on unknown intraminute
  ordering.
- Selection: at most one side per market, choosing the candidate with the
  largest prior pullback.

## Live-First Correction

The first pass selected the largest pullback per market, which is useful for
pattern discovery but not a fair live rule because later entries in the same
15-minute market are not known yet.

Re-testing as a live scanner, taking the first qualifying entry per market,
changes the best candidate.

Best robust live candidate:

1. Wait until minute `2-3`.
2. Buy only in the higher part of the entry band: `56-60c`.
3. Require a controlled setup: current ask is no more than `10c` below its prior
   ask peak.
4. Exit at `99c`; otherwise settle.

Result:

- Trades: `648`
- Average cost: `57.5c`
- Hit 99: `63.0%`
- Average exit-at-99 EV: `+5.00c`
- Positive in all four chronological quartiles:
  `+6.12c`, `+6.59c`, `+4.33c`, `+2.97c`.
- Positive in all `10` calendar weeks tested.

Best strict-pullback variant:

- Rule: minute `2-3`, cost `56-60c`, pullback `5-20c`.
- Trades: `380`
- Hit 99: `63.4%`
- Average exit-at-99 EV: `+5.33c`.
- Positive in `9/10` calendar weeks, but one chronological quartile was
  negative.

The stricter pullback version fits the original idea better, but the broader
`0-10c` controlled setup is the more robust rule in this sample.

## Discovery Finding

The best simple pattern is not "buy the cheapest side." The cleaner pattern is:

1. Wait until minute `2-5` of the 15-minute contract.
2. The side's ask has already been higher.
3. Buy when that side has pulled back `5-10c` from its prior ask peak and closes
   in the `50-60c` band.
4. Exit at `99c`; do not ride for the last penny.

Result:

- Trades: `564`
- Average cost: `53.6c`
- Hit 99: `60.5%`
- Settle win: `60.1%`
- Average exit-at-99 EV: `+6.22c`
- Average hold-to-settlement EV: `+6.47c`
- Median time from entry to 99c: `10 minutes`

The broader minute `2-8`, `5-10c` pullback rule had more trades and still held:

- Trades: `712`
- Average cost: `53.7c`
- Hit 99: `60.1%`
- Average exit-at-99 EV: `+5.82c`
- Positive in all four chronological quartiles:
  `+9.48c`, `+3.61c`, `+4.10c`, `+6.09c`.

## What To Avoid

- Do not blindly buy large crashes. Early minute `2-3` pullbacks over `20c`
  were bad: `42` trades, `40.5%` hit 99, `-12.74c` EV.
- Late entries lose urgency. Minute `12-15` selected entries were negative:
  `369` trades, `53.9%` hit 99, `-0.85c` EV.
- The lowest entry prices were not the best. Within the minute `2-8`, `5-10c`
  pullback rule:
  - `50-53c`: `384` trades, `54.2%` hit 99, `+2.11c` EV.
  - `53-56c`: `188` trades, `64.4%` hit 99, `+8.80c` EV.
  - `56-60c`: `140` trades, `70.7%` hit 99, `+11.99c` EV.

That says the edge is likely continuation after a controlled pullback, not
catching a falling side at the cheapest possible price.

## Side Bias

For the minute `2-8`, `5-10c` pullback rule:

- NO: `358` trades, `61.7%` hit 99, `+7.63c` EV.
- YES: `354` trades, `58.5%` hit 99, `+3.99c` EV.

NO was better in this sample, but both sides were positive.

## Practical Rule Draft

Candidate live scanner rule:

```text
For each BTC15 market after minute 1:
  For YES and NO:
    side_ask = YES ask, or 1 - YES bid for NO
    prior_peak = max(side_ask closes before current minute)
    pullback = prior_peak - current side_ask close

    Enter only if:
      current minute is 2-5
      current side_ask close is 0.50-0.60
      pullback is 0.05-0.10
      no position already selected for this market

    Exit:
      place/seek exit at 0.99
      otherwise settle
```

## Caveats

- This uses candle close as the entry proxy, not order-book fill simulation.
- Fees and slippage are not deducted. The broader `2-8`, `5-10c` rule remains
  positive by about `+1.82c` even after a rough `4c` total cost assumption, but
  exact Kalshi fee math should be added before sizing.
- The result should be validated on fresh data after `2026-08-20` before being
  treated as live edge.
