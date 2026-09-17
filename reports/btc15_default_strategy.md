# BTC15 Default Three-Lane Strategy

Created September 1, 2026 from the local June 15-August 20 BTC15 research set.
These are the default strategies in monitor, paper, and live modes.

## Lanes

| Lane | Backtest trades | Signals/day | Avg entry | Target | Hit target | Gross EV | Est. fee | EV after fee |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Reclaim-70, early low 15-30c | 60 | 0.90 | 44.35c | 70c | 80.0% | +11.65c | 3.17c | +8.48c |
| BTC-Fade-90, YES | 58 | 0.87 | 52.14c | 90c | 67.2% | +8.38c | 2.36c | +6.02c |
| NO-Reclaim-80 | 44 | 0.66 | 58.73c | 80c | 86.4% | +10.36c | 2.80c | +7.57c |

Combined: 162 historical signals in 66.95 days, or approximately 2.42 signals
per calendar day. The BTC-return regimes are mutually exclusive, so the lane
counts do not collide in the same market.

Fee estimates use the general Kalshi quadratic taker formula on both entry and
exit. They do not include slippage. The paper ledger calculates this fee per
simulated order and retains old flat-6.5c records unchanged.

## Run

```bash
venv/bin/python scripts/btc15_live_monitor.py --paper
venv/bin/python scripts/btc15_live_monitor.py --live
```

Keep the three strategies separate in the log. Compare paper and small-size
live fill quality, hit rate, and net EV against each lane's research baseline.
