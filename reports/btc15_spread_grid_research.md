# BTC15 Spread Grid Research

Generated from `scripts/btc15_spread_grid_research.py` using local `KXBTC15M`
1-minute candle data from `2026-06-15` through `2026-08-20`.

## Test Rules

- Tested both YES and NO.
- Entry limits: `20c` through `35c`.
- Exit targets: `30c` through `70c`.
- Buys only allowed during minutes `1-10`.
- After a fill, exits are allowed later in the contract.
- Same-candle exits are ignored.
- Main risk model: if the target never appears, score the trade as full entry
  loss.

This matches the spread-bot idea of risking `25c` to make `30c`, or risking
`30c` to make `40-50c`, without using final settlement as the edge.

## Hard Result

No blind resting-bid spread passed the robust filter:

```text
trades >= 250
hit rate > 50%
average EV > 0
all four chronological quartiles EV > 0
```

The best raw full-loss EV was still negative:

- Side: `NO`
- Entry: `32c`
- Target: `68c`
- Trades: `4,060`
- Hit rate: `45.3%`
- Breakeven hit rate: `47.1%`
- Average EV: `-1.20c`
- Chronological quartile EV: `-0.24c`, `-0.65c`, `-1.18c`, `-2.72c`

## User Example Spreads

Blind resting-bid results, both sides combined:

| Entry | Target | Trades | Hit Rate | Breakeven | EV |
|---:|---:|---:|---:|---:|---:|
| `25c` | `30c` | `6,698` | `69.2%` | `83.3%` | `-4.25c` |
| `25c` | `40c` | `6,698` | `53.3%` | `62.5%` | `-3.67c` |
| `25c` | `50c` | `6,698` | `43.7%` | `50.0%` | `-3.13c` |
| `30c` | `40c` | `7,678` | `65.3%` | `75.0%` | `-3.90c` |
| `30c` | `50c` | `7,678` | `53.6%` | `60.0%` | `-3.21c` |
| `30c` | `60c` | `7,678` | `45.3%` | `50.0%` | `-2.84c` |

Side-specific checks did not rescue the idea. NO was sometimes slightly better
than YES, but still negative under the full-risk model.

## Reclaim Trap

The data does contain very strong-looking reclaim subsets. Example:

- Entry: `30c`
- Keep only fills where the fill candle closes `40c+`
- Target: `60c`
- Kept-fill hit rate: `74.0%`
- Kept-fill EV: about `+14.4c`

But this is not a complete trade rule by itself. A resting bid at `30c` also
gets filled on the non-reclaim candles. When those bad fills are charged to the
policy and cut at the fill candle bid, the full strategy is still negative:

- Entry: `30c`
- Target: `60c`
- Keep if fill candle closes `40c+`; otherwise cut at fill candle bid
- Trades: `7,678`
- Kept rate: `11.1%`
- Kept-fill target hit: `74.0%`
- Overall win rate: `31.3%`
- Average EV: `-2.00c`
- Quartile EV: `-1.89c`, `-1.95c`, `-2.16c`, `-2.01c`

The same problem shows up at `25c`:

- Entry: `25c`
- Target: `50c`
- Keep if fill candle closes `40c+`; otherwise cut at fill candle bid
- Trades: `6,698`
- Kept rate: `5.3%`
- Kept-fill target hit: `88.3%`
- Overall win rate: `30.0%`
- Average EV: `-1.66c`

## Conclusion

The data does **not** support a blind both-side spread bot that rests bids in
the `20-35c` range during the first 10 minutes and waits for `30-70c` exits.

What the data does support is narrower:

- Reclaims after a sweep are real.
- The reclaim subset is strong.
- But the bot must avoid or cheaply reject the non-reclaim fills.

That means the next bot should not simply rest bids on both sides. It needs a
live trigger that detects the sweep/reclaim before committing, or it needs a
microstructure rule that can place/cancel bids only during the few seconds when
the fill is likely to reclaim.

Until that trigger exists, the spread-bot edge is not tradeable by the numbers.
