"""Tests for the book-based, fee-aware threshold signal (Oct 2026 revision)."""
import pytest

from bot.threshold_trader import (
    MAX_SPREAD,
    score_threshold_signal,
    taker_fee_per_share,
)

BASE = dict(spot=86_000.0, threshold=84_000.0, is_above=True, t_hours=12.0, sigma_daily=0.02)


def test_fee_curve_matches_crypto_v2():
    assert taker_fee_per_share(0.5) == pytest.approx(0.07 * 0.25)
    assert taker_fee_per_share(0.9) == pytest.approx(0.07 * 0.09)
    assert taker_fee_per_share(0.0) == 0.0
    assert taker_fee_per_share(1.0) == 0.0


def test_phantom_mid_on_empty_book_is_rejected():
    # Real case: deep-ITM market displayed at 0.57 with bid 0.13 / ask 1.00.
    assert score_threshold_signal(**BASE, best_bid=0.13, best_ask=0.999) is None


def test_missing_book_is_rejected():
    assert score_threshold_signal(**BASE, best_bid=None, best_ask=0.8) is None


def test_wide_spread_rejected():
    assert score_threshold_signal(**BASE, best_bid=0.70, best_ask=0.70 + MAX_SPREAD + 0.01) is None


def test_huge_disagreement_is_distrusted():
    # Model says ~0.9 for YES; a tight book at 0.55 means our inputs are wrong.
    assert score_threshold_signal(**BASE, best_bid=0.54, best_ask=0.55) is None


def test_outside_time_window_rejected():
    kw = {**BASE, "t_hours": 48.0}
    assert score_threshold_signal(**kw, best_bid=0.70, best_ask=0.71) is None


def test_edge_is_net_of_ask_and_fee():
    sig = score_threshold_signal(**BASE, best_bid=0.80, best_ask=0.81)
    assert sig is not None and sig["side"] == "YES"
    assert sig["price"] == pytest.approx(0.81)
    expected = sig["our_prob_yes"] - 0.81 - taker_fee_per_share(0.81)
    assert sig["edge"] == pytest.approx(expected)
    # blended prob sits between model and market
    assert sig["mid_yes"] < sig["our_prob_yes"] < sig["model_prob_yes"]


def test_no_side_priced_off_yes_bid():
    kw = {**BASE, "threshold": 88_000.0}  # YES unlikely => NO favourite
    sig = score_threshold_signal(**kw, best_bid=0.15, best_ask=0.16)
    assert sig is not None and sig["side"] == "NO"
    assert sig["price"] == pytest.approx(1 - 0.15)


def test_fair_market_no_trade():
    # Book close to fair value -> fee + spread eat the edge.
    assert score_threshold_signal(**BASE, best_bid=0.86, best_ask=0.87) is None
