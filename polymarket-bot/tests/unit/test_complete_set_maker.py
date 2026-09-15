from dataclasses import replace

import pytest

from bot.complete_set_maker import (
    BookTop,
    CompleteSetScanner,
    FeeSchedule,
    MakerScannerConfig,
    _maker_bid,
    evaluate_binary_market,
)


def _book(bid: float, ask: float, *, minimum: float = 5.0) -> dict:
    return {
        "bids": [{"price": str(bid), "size": "100"}],
        "asks": [{"price": str(ask), "size": "100"}],
        "tick_size": "0.01",
        "min_order_size": str(minimum),
    }


def _market() -> dict:
    return {
        "conditionId": "condition-1",
        "question": "Will the home team win?",
        "clobTokenIds": ["yes-token", "no-token"],
        "outcomes": ["Yes", "No"],
        "volume24hr": 50_000,
        "liquidityNum": 20_000,
        "feeSchedule": {"rate": 0, "exponent": 1, "rebateRate": 0},
    }


def _event(slug: str = "game-1") -> dict:
    return {"title": "Home vs Away", "slug": slug, "endDate": "2026-08-02T18:00:00Z"}


def test_v2_fee_curve() -> None:
    fee = FeeSchedule(rate=0.05, exponent=1)
    assert fee.taker_fee(shares=10, price=0.5) == pytest.approx(0.125)


def test_maker_bid_improves_only_when_post_only() -> None:
    wide = BookTop(bid=0.50, ask=0.53, tick_size=0.01, min_order_size=5)
    tight = BookTop(bid=0.50, ask=0.51, tick_size=0.01, min_order_size=5)
    assert _maker_bid(wide) == pytest.approx(0.51)
    assert _maker_bid(tight) == pytest.approx(0.50)


def test_evaluate_builds_capital_safe_pair() -> None:
    result = evaluate_binary_market(
        market=_market(),
        event=_event(),
        yes_book=_book(0.50, 0.53),
        no_book=_book(0.46, 0.49),
        bankroll=100,
        config=MakerScannerConfig(),
    )

    assert result is not None
    assert result.quote_yes == pytest.approx(0.51)
    assert result.quote_no == pytest.approx(0.47)
    assert result.gross_edge == pytest.approx(0.02)
    assert result.locked_edge_usd == pytest.approx(0.10)
    assert result.reserve_usd == pytest.approx(5.0)
    assert result.worst_emergency_loss_usd == pytest.approx(0.0)


def test_rejects_market_whose_minimum_order_is_too_large() -> None:
    result = evaluate_binary_market(
        market=_market(),
        event=_event(),
        yes_book=_book(0.50, 0.53, minimum=50),
        no_book=_book(0.46, 0.49, minimum=50),
        bankroll=100,
        config=MakerScannerConfig(),
    )
    assert result is None


def test_rejects_one_leg_adverse_selection_above_cap() -> None:
    result = evaluate_binary_market(
        market=_market(),
        event=_event(),
        yes_book=_book(0.50, 0.53),
        no_book=_book(0.46, 0.57),
        bankroll=100,
        config=MakerScannerConfig(),
    )
    assert result is None


def test_allocator_enforces_budget_and_one_market_per_event() -> None:
    base = evaluate_binary_market(
        market=_market(),
        event=_event(),
        yes_book=_book(0.50, 0.53),
        no_book=_book(0.46, 0.49),
        bankroll=100,
        config=MakerScannerConfig(),
    )
    assert base is not None

    scanner = object.__new__(CompleteSetScanner)
    scanner._bankroll_getter = lambda: 20.0
    scanner.config = MakerScannerConfig(max_total_reserve_pct=0.50, max_markets=8)
    same_event = replace(base, condition_id="condition-2", score=base.score + 1)
    other_event = replace(base, condition_id="condition-3", event_slug="game-2")
    third_event = replace(base, condition_id="condition-4", event_slug="game-3")

    selected = scanner.allocate([same_event, base, other_event, third_event])
    assert [row.condition_id for row in selected] == ["condition-2", "condition-3"]
    assert sum(row.reserve_usd for row in selected) <= 10.0
