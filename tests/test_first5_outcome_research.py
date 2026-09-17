import numpy as np
import pandas as pd

from scripts.btc15_first5_outcome_research import META_COLUMNS, score_trades, stack_features


def sample_predictions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "p_model": [0.80, 0.80],
            "yes_cash_cost": [0.50, 0.50],
            "no_cash_cost": [0.51, 0.51],
            "kalshi_outcome_up": [1, 0],
            "yes_hit80": [1, 1],
            "no_hit80": [0, 0],
        }
    )


def test_settlement_scoring_charges_costs() -> None:
    result = score_trades(sample_predictions(), "p_model", 0.20, 0, 1, 6.5, 100)

    assert result["trades"] == 2
    assert result["avg_pnl_c"] == -6.5
    assert np.isclose(result["profit_factor"], 43.5 / 56.5)
    assert result["win_rate"] == 0.5


def test_target_exit_uses_post_cutoff_touch() -> None:
    result = score_trades(sample_predictions(), "p_model", 0.20, 0, 1, 6.5, 80)

    assert result["trades"] == 2
    assert result["avg_pnl_c"] == 23.5
    assert np.isnan(result["profit_factor"])
    assert result["win_rate"] == 1.0


def test_outcome_is_excluded_from_model_features() -> None:
    assert "kalshi_outcome_up" in META_COLUMNS


def test_dual_stack_keeps_market_btc_and_kalshi_inputs_separate() -> None:
    values = stack_features(
        np.array([0.60, 0.40]),
        np.array([0.70, 0.30]),
        np.array([0.65, 0.35]),
    )

    assert values.shape == (2, 6)
    assert np.allclose(values[:, 3], [0.10, -0.10])
    assert np.allclose(values[:, 4], [0.05, -0.05])
