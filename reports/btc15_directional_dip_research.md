# BTC15 Directional Dip Research

Freeze an UP/DOWN model after minute five, buy only a later 20-40c dip in that direction, and scalp a nearer target.

- Fee/slippage allowance: `6.5c`
- Entries begin: minute `6`
- Same-candle target touches: excluded
- Discovery folds: `0-2`
- Untouched validation fold: `3`
- Repeated profitable parameter sets: `10`
- Repeated PF >= 5 parameter sets: `0`

## Best Repeated Rules

```text
         model  confidence  entry_c  entry_penetration_c  entry_end_minute  target_c  target_penetration_c  discovery_trades  discovery_avg_pnl_c  discovery_profit_factor  validation_trades  validation_avg_pnl_c  validation_profit_factor
p_market_blend       0.100       20                    0                 8        90                     2                60               2.0000                   1.1104                 22                2.1364                    1.1182
  p_btc_signal       0.150       25                    1                 8        90                     2                64               3.6562                   1.1905                 41                1.4268                    1.0714
  p_btc_signal       0.150       25                    0                 8        85                     2                72               1.5556                   1.0808                 44                1.3409                    1.0694
  p_btc_signal       0.150       25                    0                 8        90                     2                72               3.5000                   1.1818                 44                1.2273                    1.0612
p_market_blend       0.100       25                    1                 8        90                     2                82               1.4268                   1.0714                 33                1.2273                    1.0612
  p_btc_signal       0.125       20                    0                 8        90                     2                65               5.3462                   1.3122                 30                0.5000                    1.0270
p_market_blend       0.100       20                    1                 8        90                     2                52               6.3846                   1.3796                 20                0.5000                    1.0270
  p_btc_signal       0.125       25                    1                 8        90                     2                84               1.7143                   1.0863                 45                0.5000                    1.0246
p_market_blend       0.100       20                    0                 8        85                     2                60               0.4167                   1.0230                 22                0.5455                    1.0302
  p_btc_signal       0.175       25                    0                 8        90                     2                51               0.2647                   1.0130                 35                7.0714                    1.3929
```
