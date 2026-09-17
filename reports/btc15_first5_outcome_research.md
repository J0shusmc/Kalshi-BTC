# BTC15 First-Five Outcome Model

Leakage-safe expanding-window research using only information available after five completed minutes.

- Fee/slippage allowance: `6.5c` per contract
- Threshold selection: folds 0-2
- Untouched validation: fold 3
- Strategies with PF >= 5 in both periods: `0`

## Model Metrics

```text
              model    auc  brier  log_loss
           p_market 0.7975 0.1843    0.5475
     p_market_blend 0.7973 0.1844    0.5478
       p_dual_stack 0.7938 0.1854    0.5505
p_residual_lightgbm 0.7929 0.1859    0.5522
         p_ensemble 0.7906 0.1870    0.5553
          p_xgboost 0.7900 0.1871    0.5559
            p_logit 0.7867 0.1884    0.5573
    p_kalshi_signal 0.7864 0.1886    0.5585
         p_lightgbm 0.7866 0.1887    0.5596
          p_hist_gb 0.7864 0.1888    0.5600
       p_btc_signal 0.7798 0.1917    0.5679
```

## Best Repeated Trade Rules

```text
No rule was profitable in both discovery and validation.
```

PF 5 is a research target, not a reason to accept a small or unstable sample.
