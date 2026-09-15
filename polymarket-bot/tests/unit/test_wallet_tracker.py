from datetime import timezone

import pytest

from data.wallet_tracker import WalletTracker, _parse_timestamp


def test_parse_unix_timestamp_as_actual_time() -> None:
    parsed = _parse_timestamp(1_722_518_400)
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat().startswith("2024-08-01")


def test_parse_iso_timestamp() -> None:
    parsed = _parse_timestamp("2026-08-01T12:30:00Z")
    assert parsed.isoformat() == "2026-08-01T12:30:00+00:00"


def test_activity_size_is_converted_from_tokens_to_usd() -> None:
    trade = WalletTracker._parse_activity(
        "0xabc",
        {
            "conditionId": "condition-1",
            "asset": "token-1",
            "side": "BUY",
            "price": 0.25,
            "size": 100,
            "timestamp": 1_722_518_400,
        },
    )
    assert trade is not None
    assert trade.size_usd == pytest.approx(25.0)
    assert trade.timestamp.isoformat().startswith("2024-08-01")
