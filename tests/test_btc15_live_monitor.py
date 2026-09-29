import datetime as dt
from scripts.btc15_live_monitor import (
    BtcContext,
    ActivePosition,
    BotStats,
    NO_RECLAIM_STRATEGY,
    PaperAccountStats,
    RECLAIM_STRATEGY,
    ReclaimState,
    SideQuote,
    build_reclaim_states,
    estimated_taker_fee_cents,
    manage_paper_trades,
    print_snapshot,
    reconcile_pending_orders,
    selected_signals,
    sync_open_positions,
)


def test_general_taker_fee_matches_quadratic_schedule() -> None:
    assert estimated_taker_fee_cents(50, 100) == 175.0
    assert estimated_taker_fee_cents(70, 100) == 147.0


def quote(side: str, bid: float, ask: float) -> SideQuote:
    return SideQuote(
        side=side,
        bid=bid,
        bid_size=10,
        ask=ask,
        ask_size=10,
        prior_peak_ask=None,
        prior_low_ask=None,
        prior_close_ask=None,
        last_ask_open=None,
        last_ask_close=None,
        last_ask_low=None,
        last_ask_high=None,
        last_range=None,
        last_body=None,
        drop_from_prior_high=None,
        pullback=None,
        live_match=False,
        strict_match=False,
        reclaim_signal=False,
        reclaim_name="",
    )


def ready_state(strategy: str, side: str, entry: int, target: int) -> ReclaimState:
    return ReclaimState(
        strategy=strategy,
        display_strategy=strategy,
        side=side,
        minute=7,
        early_ask_low=0.27,
        decision_ask_close=0.30,
        entry_ask_open=0.45,
        entry_ask_close=entry / 100,
        live_ask=entry / 100,
        btc_context=BtcContext(True),
        checks=[("ready", True)],
        ready=True,
        entry_cents=entry,
        target_cents=target,
    )


def test_default_states_keep_their_own_side_and_target() -> None:
    states = [
        ready_state(RECLAIM_STRATEGY, "YES", 52, 70),
        ready_state(NO_RECLAIM_STRATEGY, "NO", 59, 80),
    ]
    signals = selected_signals("KXBTC15M-TEST", 5, states)

    assert [(s.strategy, s.side, s.target_cents) for s in signals] == [
        (RECLAIM_STRATEGY, "yes", 70),
        (NO_RECLAIM_STRATEGY, "no", 80),
    ]


def test_default_builder_has_two_lanes() -> None:
    sides = [quote("YES", 0.49, 0.51), quote("NO", 0.49, 0.51)]
    context = BtcContext(False, error="not loaded")

    states = build_reclaim_states("TEST", sides, [], 0, context, True)

    assert [state.strategy for state in states] == [
        RECLAIM_STRATEGY,
        NO_RECLAIM_STRATEGY,
    ]


def test_filled_pending_entry_keeps_its_strategy_target_during_sync() -> None:
    """A just-filled NO_RECLAIM_80 order must not get the generic 70c exit."""
    ticker = "KXBTC15M-TEST"
    order_id = "entry-order"
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    position = ActivePosition(ticker, "NO", 5, 330, 66, 350, 20, 0, 0)
    log = {
        "active_positions": {},
        "pending_orders": {
            order_id: {
                "type": "buy",
                "signal_key": f"{ticker}_no_{NO_RECLAIM_STRATEGY}",
                "ticker": ticker,
                "side": "no",
                "count": 5,
                "price_cents": 66,
                "target_cents": 80,
                "strategy": NO_RECLAIM_STRATEGY,
            }
        },
        "trades": [],
    }

    sync_open_positions(log, [position], now)

    assert log["active_positions"] == {}
    reconcile_pending_orders(None, log, ticker, [position], [], now)
    active = log["active_positions"][f"{ticker}_no_{NO_RECLAIM_STRATEGY}"]
    assert active["strategy"] == NO_RECLAIM_STRATEGY
    assert active["target_cents"] == 80

def test_paper_exit_uses_position_target_not_legacy_global_target() -> None:
    key = "KXBTC15M-TEST_yes_RECLAIM_70_paper"
    log = {
        "paper_positions": {
            key: {
                "ticker": "KXBTC15M-TEST",
                "side": "yes",
                "strategy": RECLAIM_STRATEGY,
                "count": 5,
                "entry_cents": 52,
                "target_cents": 90,
            }
        },
        "paper_entered_signals": {},
        "paper_trades": [],
    }
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

    manage_paper_trades(log, "KXBTC15M-TEST", [quote("YES", 0.85, 0.86)], [], now)
    assert key in log["paper_positions"]

    manage_paper_trades(log, "KXBTC15M-TEST", [quote("YES", 0.90, 0.91)], [], now)
    assert key not in log["paper_positions"]
    assert log["paper_trades"][-1]["price_cents"] == 90


def render_snapshot(capsys, *, live: bool, paper: bool) -> str:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    stats = BotStats(3, 2, 1, 100, 300, 100, -100, None, -100, 2.0)
    paper_stats = PaperAccountStats(0, 0, 0, 0, 0, 0, 0, 0, 0, 11_500, 0, None, None, None)
    print_snapshot(
        {"balance": 11_500, "portfolio_value": 11_500},
        {"open_time": now.isoformat(), "close_time": (now + dt.timedelta(minutes=15)).isoformat()},
        [],
        [],
        [],
        [],
        stats,
        paper_stats,
        None,
        [],
        live,
        paper,
        {},
        5,
        10.0,
        11_500,
        now,
        color=False,
    )
    return capsys.readouterr().out


def test_mode_row_is_directly_below_countdown(capsys) -> None:
    output = render_snapshot(capsys, live=False, paper=True)
    lines = output.splitlines()
    countdown_index = next(i for i, line in enumerate(lines) if "BTC 15 mins" in line)
    mode_line = lines[countdown_index + 1]
    expected = "PAPER | risk 10% cash/signal"

    assert mode_line.strip() == expected
    assert mode_line.index("PAPER") == (55 - len(expected)) // 2


def test_live_and_paper_hide_each_others_position_sections(capsys) -> None:
    live_output = render_snapshot(capsys, live=True, paper=False)
    paper_output = render_snapshot(capsys, live=False, paper=True)

    assert "Active Orders" in live_output
    assert "--- Paper " not in live_output
    assert "--- Paper " in paper_output
    assert "Active Orders" not in paper_output


def test_real_account_stats_visible_in_live_and_monitor_modes(capsys) -> None:
    for live in (False, True):
        output = render_snapshot(capsys, live=live, paper=False)
        assert "2W-1L | win 66.7%" in output
        assert "Live Stats" in output
        assert "Active Orders" in output
        assert "Paper" not in output
        assert "PAPER" not in output


def test_paper_mode_displays_only_paper_performance(capsys) -> None:
    output = render_snapshot(capsys, live=False, paper=True)
    assert "Paper Stats" in output
    assert "0W-0L | win 0.0%" in output
    assert "2W-1L" not in output
    assert "Live Stats" not in output
