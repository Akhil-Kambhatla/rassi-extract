import pytest

from rassi.render import parse_page_selection
from rassi.spend import SpendTracker, TokenUsage, cost_usd


def test_cost_uses_price_table():
    assert cost_usd("gpt-5.6-terra", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)) == pytest.approx(14.00)
    assert cost_usd("gpt-5.6-luna", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)) == pytest.approx(1.40)


def test_tracker_stops_before_exceeding_limit():
    tracker = SpendTracker("gpt-5.6-terra", max_spend_usd=0.05, fallback_estimate_usd=0.02)
    first = tracker.try_reserve_call()
    second = tracker.try_reserve_call()
    assert first == second == 0.02
    assert tracker.try_reserve_call() is None
    assert tracker.limit_reached


def test_tracker_records_actual_usage():
    tracker = SpendTracker("gpt-5.6-terra", max_spend_usd=1.0, fallback_estimate_usd=0.01)
    reservation = tracker.try_reserve_call()
    call_cost, running = tracker.record_call(reservation, TokenUsage(input_tokens=1000, output_tokens=500, reasoning_tokens=200))
    assert call_cost == pytest.approx(0.002 + 0.006)
    assert running == pytest.approx(call_cost)
    assert tracker.reserved_usd == 0
    assert tracker.usage.reasoning_tokens == 200
    assert tracker.next_call_estimate_usd() == pytest.approx(call_cost)


@pytest.mark.parametrize(
    "selection, expected",
    [(None, [1, 2, 3, 4, 5, 6]), ("1-4", [1, 2, 3, 4]), ("5", [5]), ("1-2,5-6", [1, 2, 5, 6])],
)
def test_page_selection(selection, expected):
    assert parse_page_selection(selection, 6) == expected


@pytest.mark.parametrize("selection", ["0-2", "5-9", "4-2", ""])
def test_page_selection_rejects_bad_ranges(selection):
    with pytest.raises(ValueError):
        parse_page_selection(selection, 6)
