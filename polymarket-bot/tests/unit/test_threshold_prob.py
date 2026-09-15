"""Unit tests for the BS binary-option probability used by threshold_trader."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """Import threshold_trader without pulling heavy app deps."""
    root = Path(__file__).resolve().parents[2]
    for name in ("data.binance_ws", "storage.repository", "trading.portfolio"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
    sys.modules["data.binance_ws"].BinanceWSFeed = type("BinanceWSFeed", (), {})
    sys.modules["storage.repository"].TradeRepository = type("TradeRepository", (), {})
    sys.modules["trading.portfolio"].Portfolio = type("Portfolio", (), {})
    spec = importlib.util.spec_from_file_location(
        "threshold_trader_under_test", root / "bot" / "threshold_trader.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = _load_module()
prob_above = tt.prob_above


class TestProbAbove:
    def test_at_the_money_24h(self):
        # S = K, 24h, 3% daily vol → ~50% (slight downward drift from -0.5σ²T)
        p = prob_above(70_000, 70_000, 24, 0.03)
        assert 0.45 < p < 0.50

    def test_in_the_money_short_horizon(self):
        # +2.9% above K with very low vol-time → ~certain
        p = prob_above(70_000, 68_000, 0.5, 0.03)
        assert p > 0.99

    def test_in_the_money_long_horizon(self):
        # +2.9% above K but 5d horizon → much less certain
        p = prob_above(70_000, 68_000, 120, 0.03)
        assert 0.55 < p < 0.75

    def test_out_of_the_money_24h(self):
        # K above S → low probability
        p = prob_above(68_000, 70_000, 24, 0.03)
        assert p < 0.20

    def test_T_zero_above(self):
        assert prob_above(70_000, 68_000, 0, 0.03) == 1.0

    def test_T_zero_below(self):
        assert prob_above(68_000, 70_000, 0, 0.03) == 0.0

    def test_sigma_zero(self):
        # σ=0 → deterministic by current relation
        assert prob_above(70_000, 68_000, 24, 0.0) == 1.0
        assert prob_above(68_000, 70_000, 24, 0.0) == 0.0

    def test_degenerate_inputs_safe(self):
        # No exception, deterministic fallback via S > K check
        assert prob_above(0, 70_000, 24, 0.03) == 0.0
        assert prob_above(70_000, 0, 24, 0.03) == 1.0

    def test_monotonic_in_S(self):
        prev = -1
        for S in (65_000, 67_000, 68_000, 69_000, 70_000, 72_000):
            p = prob_above(S, 68_000, 24, 0.03)
            assert p >= prev
            prev = p

    def test_monotonic_in_T_for_otm(self):
        # For OTM (S < K), longer T → higher prob (can drift up)
        p_short = prob_above(67_000, 70_000, 1, 0.03)
        p_long = prob_above(67_000, 70_000, 96, 0.03)
        assert p_long > p_short

    def test_monotonic_in_T_for_itm(self):
        # For deep ITM, longer T → lower prob (can drift down)
        p_short = prob_above(72_000, 70_000, 1, 0.03)
        p_long = prob_above(72_000, 70_000, 96, 0.03)
        assert p_short > p_long

    def test_sigma_inverse_relation_atm(self):
        # ATM: higher sigma, prob still ≈ 50% (symmetric drift dominates barely)
        p_low = prob_above(70_000, 70_000, 24, 0.01)
        p_high = prob_above(70_000, 70_000, 24, 0.10)
        assert 0.40 < p_low < 0.50
        assert 0.40 < p_high < 0.50

    def test_realistic_btc_signal_matches_old_step(self):
        # Old step function: distance >2% → 0.85 prob.
        # BS @ +2.9%, 24h, 3% vol should be in same ballpark (0.80–0.90).
        p = prob_above(70_000, 68_000, 24, 0.03)
        assert 0.78 < p < 0.90


class TestKellyMultiplier:
    def test_kelly_buckets(self):
        Trader = tt.ThresholdTrader
        # We don't need full init; method is pure.
        instance = Trader.__new__(Trader)
        # ITM-only mode (since Apr 17 rewrite):
        # _KELLY_BY_PRICE = [(0.60, 0.25), (0.75, 0.35), (0.90, 0.50), (1.00, 0.40)]
        # price < first cutoff → multiplier. Below 0.50 prices are filtered by
        # MIN_PRICE_ENTRY upstream, so this only covers the accepted range.
        assert instance._kelly_multiplier(0.55) == 0.25
        assert instance._kelly_multiplier(0.65) == 0.35
        assert instance._kelly_multiplier(0.85) == 0.50
        assert instance._kelly_multiplier(0.92) == 0.40
        # sanity check edge cases: below smallest cutoff still returns smallest k
        assert instance._kelly_multiplier(0.10) == 0.25


class TestParseQuestion:
    def test_btc_above(self):
        r = tt._parse_threshold_question(
            "Will the price of Bitcoin be above $70,000 on April 14?"
        )
        assert r["asset_key"] == "BTC"
        assert r["direction"] == "above"
        assert r["threshold"] == 70_000.0
        assert r["binance_symbol"] == "BTCUSDT"

    def test_eth_below_decimal(self):
        r = tt._parse_threshold_question(
            "Will Ethereum be below $2,150.50 on May 1?"
        )
        assert r["asset_key"] == "ETH"
        assert r["direction"] == "below"
        assert r["threshold"] == 2150.5

    def test_doge(self):
        r = tt._parse_threshold_question(
            "Will the price of Dogecoin be above $0.25 on April 20?"
        )
        assert r["asset_key"] == "DOGE"
        assert r["binance_symbol"] == "DOGEUSDT"

    def test_no_match(self):
        assert tt._parse_threshold_question("Will Trump win 2028?") is None
