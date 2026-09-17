import numpy as np
import pandas as pd

from scripts.btc15_directional_dip_research import score_selected, select_directional


def test_directional_selection_uses_the_predicted_side() -> None:
    candidates = pd.DataFrame(
        {
            "side": ["YES", "NO", "YES", "NO"],
            "p_model": [0.70, 0.70, 0.30, 0.30],
        }
    )

    selected = select_directional(candidates, "p_model", 0.15)

    assert selected.index.tolist() == [0, 3]


def test_target_and_settlement_losses_are_scored_after_fees() -> None:
    selected = pd.DataFrame(
        {
            "max_future_bid_c": [92, 50],
            "settle_win": [0, 0],
            "fill_minute": [7, 8],
        }
    )

    result = score_selected(selected, 25, 90, 1, 6.5)

    assert result["trades"] == 2
    assert result["avg_pnl_c"] == 13.5
    assert np.isclose(result["profit_factor"], 58.5 / 31.5)
    assert result["win_rate"] == 0.5
