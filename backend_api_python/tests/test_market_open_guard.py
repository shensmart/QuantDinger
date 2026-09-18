"""The session guard that keeps scheduled AI monitors from running after the close."""

from datetime import datetime, timezone

from app.services.market_schedule import is_market_open
from app.services.portfolio_monitor import _market_closed, _market_of


def _shanghai(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def test_a_share_session_is_open_only_between_open_and_close():
    assert is_market_open("CNStock", _shanghai("2026-09-18 11:00:00+08:00")) is True
    assert is_market_open("CNStock", _shanghai("2026-09-18 19:50:00+08:00")) is False
    assert is_market_open("CNStock", _shanghai("2026-09-18 09:20:00+08:00")) is False


def test_closed_weekend_and_continuous_or_unknown_markets():
    assert is_market_open("CNStock", _shanghai("2026-09-19 11:00:00+08:00")) is False
    assert is_market_open("Crypto") is True
    assert is_market_open("NotAMarket") is True
    assert is_market_open("") is True


def test_unknown_or_mixed_markets_are_never_muted():
    assert _market_closed([{"market": "Crypto"}], {"market": "Crypto"}) is False
    assert _market_closed([{"market": ""}], {}) is False
    assert _market_closed([{"market": "CNStock"}, {"market": "USStock"}], {}) is False


def test_calendar_past_its_last_session_never_mutes_a_monitor():
    # exchange_calendars raises MinuteOutOfBounds beyond its bundled range; that
    # must read as "open", not as a monitor that silently stops forever.
    assert is_market_open("CNStock", _shanghai("2027-06-01 11:00:00+08:00")) is True
def test_configured_market_wins_and_mismatched_positions_do_not_mute():
    closed = _shanghai("2026-09-18 19:50:00+08:00")
    assert _market_of([{"market": "USStock"}], {"market": "CNStock"}) == "CNStock"
    assert _market_closed([{"market": "USStock"}], {"market": "CNStock"}, now=closed) is True
