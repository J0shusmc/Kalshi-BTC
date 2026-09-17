# BTC15 Reclaim Edge Research

Generated from local `KXBTC15M` 1-minute candle data from `2026-06-15`
through `2026-08-20`.

This is the first spread-style pattern that held up after charging the strategy
for realistic losers.

## What Failed

Blind resting bids in the `20-35c` range did not work.

Examples, both sides combined:

- `25c -> 30c`: `69.2%` hit, but breakeven is `83.3%`; EV `-4.25c`.
- `25c -> 50c`: `43.7%` hit, breakeven `50.0%`; EV `-3.13c`.
- `30c -> 50c`: `53.6%` hit, breakeven `60.0%`; EV `-3.21c`.
- `30c -> 60c`: `45.3%` hit, breakeven `50.0%`; EV `-2.84c`.

The data says the edge is not the fill in the 20s by itself. The edge is the
reclaim after a prior sweep into the low 20s.

## Candidate Edge

Trade only during minutes `2-10`. After minute `10`, no new buys; exits only.

Use side-specific rules:

### NO Momentum Reclaim

Buy NO if:

- Side is `NO`.
- Current minute is `2-10`.
- Current spread is `<= 2c`.
- Prior ask low for this side was `<= 24.5c`.
- Current candle ask low is `> 25.5c`.
- Current candle range is `<= 25.5c`.
- Current ask body is `> 14.5c`.
- Current ask close is `50-65c`.
- Current ask close is no more than `51.45c` below the prior ask high.

Target:

- Sell at `85c`.

Result buying at the signal candle ask close:

- Trades: `110`
- Hit target: `81.8%`
- Average entry: `56.1c`
- Average EV: `+13.41c`
- Chronological quartile EV: `+17.64c`, `+8.67c`, `+9.70c`, `+17.32c`
- Positive weeks: `10/10`
- Worst week: `+1.17c`

### YES Sweep Reclaim

Buy YES if:

- Side is `YES`.
- Current minute is `2-10`.
- Current spread is `<= 2c`.
- Prior ask low for this side was `<= 24.5c`.
- Current candle ask low is `> 23.5c` and `<= 32.5c`.
- Current candle range is `> 25.5c`.
- Current ask close is `45-65c`.
- Current ask close is no more than `51.45c` below the prior ask high.

Target:

- Sell at `85c`.

Result buying at the signal candle ask close:

- Trades: `144`
- Hit target: `75.0%`
- Average entry: `55.4c`
- Average EV: `+8.38c`
- Chronological quartile EV: `+8.72c`, `+15.03c`, `+1.53c`, `+8.22c`
- Positive weeks: `10/10`
- Worst week: `+1.00c`

## Combined Bot Rule

Use both side-specific rules, at most one trade per market.

Signal-candle close entry:

- Trades: `253`
- Hit target: `78.3%`
- Average entry: `55.7c`
- Average EV: `+10.80c`
- Chronological quartile EV: `+12.62c`, `+12.48c`, `+4.86c`, `+13.21c`
- Positive weeks: `10/10`
- Worst week: `+2.92c`

Next-minute open entry proxy:

- Trades: `209`
- Hit target: `79.4%`
- Average entry: `55.6c`
- Average EV: `+11.90c`
- Chronological quartile EV: `+12.02c`, `+13.98c`, `+6.79c`, `+14.81c`
- Positive weeks: `10/10`
- Worst week: `+0.58c`

## Interpretation

This is not a `25c -> 30c` spread scalper. It is a post-sweep reclaim bot:

- Wait for one side to get washed into the low 20s earlier in the contract.
- Do not buy the dead low.
- Buy only after that side reclaims into the mid-50s with the right candle
  shape.
- Target an `85c` bid.
- No new buys after minute `10`.

## Caveats

- This uses 1-minute candle OHLC, not tick-level order book replay.
- The close-entry result assumes a fill near the signal candle close.
- The next-open proxy held up, but live slippage and Kalshi fees still need to
  be modeled before trading.
- Sample size is useful but not huge: `253` combined signal-close trades.

This is the strongest candidate found so far in the provided data.
