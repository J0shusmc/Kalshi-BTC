# BTC15 Dip-to-60 Spread Research

Generated from `scripts/btc15_dip_to_60_research.py` using local `KXBTC15M`
1-minute candle data from `2026-06-15` through `2026-08-20`.

This ignores settlement and 99c exits. It only asks:

```text
If YES or NO dips into the 20s during the first 10 minutes,
how often does that side later show a 60c+ bid?
```

## Definitions

- `close` mode: first candle in minutes `1-10` where side ask closes from
  `20-30c`.
- `touch` mode: first candle in minutes `1-10` where side ask low touches
  `20-30c`, even if it closes higher.
- Rebound target: later side bid high reaches `60c+`.
- Same-candle rebound is ignored; the target must appear after the dip candle.

For NO, side prices are derived from YES quotes:

```text
NO ask = 1 - YES bid
NO bid = 1 - YES ask
```

## Main Stats

Close-confirmed 20s dips:

- Setups: `5,471`
- Later 50c+ bid: `44.8%`
- Later 60c+ bid: `37.8%`
- Average max future bid: `56.0c`
- Average dip price: `25.3c`
- Median spread at dip close: `1.0c`
- Median minutes to 60c bid, when it hits: `4`

Intraminute 20s touches:

- Setups: `6,366`
- Later 50c+ bid: `57.6%`
- Later 60c+ bid: `48.6%`
- Average max future bid: `64.0c`
- Average touched dip price: `25.5c`
- Average ask close after touch: `31.6c`
- Median spread at dip close: `1.0c`
- Median minutes to 60c bid, when it hits: `4`

## Best Spread-Bot Shape

The stronger setup is not a side that stays dead in the 20s. It is a fast sweep
into the 20s that snaps back by the candle close.

Touch `26-30c` during minutes `1-4`:

- Setups: `2,209`
- Later 50c+ bid: `62.1%`
- Later 60c+ bid: `53.2%`
- Average max future bid: `67.1c`
- Average dip price: `27.6c`
- Average spread at close: `1.03c`
- Positive/stable across chronological quartiles:
  - `52.3%`
  - `54.7%`
  - `52.9%`
  - `52.7%`

Touch `20-30c` and close back `30-50c`:

- Setups: `2,878`
- Later 50c+ bid: `65.6%`
- Later 60c+ bid: `54.7%`
- Average max future bid: `68.9c`
- Average touched dip price: `26.6c`
- Stable across chronological quartiles:
  - `55.7%`
  - `56.2%`
  - `54.2%`
  - `52.5%`

Touch `20-30c` and close back `40-60c`:

- Setups: `807`
- Later 50c+ bid: `85.9%`
- Later 60c+ bid: `72.5%`
- Average max future bid: `79.4c`
- Average touched dip price: `26.4c`
- Stable across chronological quartiles:
  - `76.7%`
  - `70.8%`
  - `71.6%`
  - `70.8%`

That last setup is the cleanest evidence of reclaim behavior, but it may be
harder to fill in live trading unless the bot already had resting bids in the
20s before the snapback.

## What Not To Build Around

Close-confirmed 20s dips are weaker:

- Later 60c+ bid only `37.8%`.
- Even the better `26-30c` close band only reached `42.2%`.
- The `20-23c` close band was worst: `32.1%` hit a 60c bid.

So a bot that waits until the candle closes in the 20s is probably late and
catching weak contracts. A spread bot should be trying to rest or react during
fast sweeps, not after the market has already accepted the 20s price.

## First Bot Rule Draft

Monitor both YES and NO during minutes `1-10`, but prioritize minutes `1-4`.

Candidate setup:

```text
For each side:
  if current ask/low touches 26-30c:
    note the sweep
  if the same 1-minute candle closes 30-50c:
    mark as reclaim candidate
  if resting bid was filled in the 20s:
    offer/seek exit around 50-60c depending on liquidity
```

Aggressive target:

- Exit bid target: `60c`
- Historical hit rate from touch/reclaim `30-50c`: `54.7%`

More conservative target:

- Exit bid target: `50c`
- Historical hit rate from touch/reclaim `30-50c`: `65.6%`

## Bot Implication

This likely replaces the prior `56-60c` entry monitor. The prior monitor is a
momentum entry scanner. This new idea is a spread/reversion bot:

- It should track intraminute lows, not only current ask.
- It should maintain a recent sweep state for each side.
- It should care about fillable bid/ask spread and available size.
- It should report target ladder probability: `50c`, `55c`, `60c`, `65c`, `70c`.

No trading logic should be enabled until we add live fill simulation and fee
math.
