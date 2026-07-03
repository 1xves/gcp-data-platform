"""
test_aggregate.py — unit tests for transforms/aggregate.py

Covers:
  - ExtractWindowKeyDo: (user_id, event_type) keying, session_id fallback
  - EventAggregationFn: the full CombineFn lifecycle (create / add / merge /
    extract), value handling, distinct-session counting
  - FormatAggregateRowDo: BigQuery row shape with injected window bounds

build_aggregation_pipeline() is intentionally not unit-tested: it is pipeline
assembly (WindowInto + WriteToBigQuery wiring) that only executes meaningfully
on a runner; its behavior is covered by the load-test plan, not unit tests.
"""

from transforms.aggregate import (
    EventAggregationFn,
    ExtractWindowKeyDo,
    FormatAggregateRowDo,
)


def make_event(**overrides):
    event = {
        "event_id": "evt-1",
        "event_type": "purchase",
        "user_id": "user-1",
        "session_id": "sess-1",
        "value_usd": None,
    }
    event.update(overrides)
    return event


# ─────────────────────────────────────────────────────────────────────────────
# ExtractWindowKeyDo
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractWindowKey:
    def test_keys_on_user_and_event_type(self):
        event = make_event()
        outputs = list(ExtractWindowKeyDo().process(event))

        assert outputs == [(("user-1", "purchase"), event)]

    def test_anonymous_user_falls_back_to_session(self):
        event = make_event(user_id=None, session_id="sess-9")
        outputs = list(ExtractWindowKeyDo().process(event))

        assert outputs == [(("sess-9", "purchase"), event)]


# ─────────────────────────────────────────────────────────────────────────────
# EventAggregationFn — CombineFn lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestEventAggregationFn:
    def setup_method(self):
        self.fn = EventAggregationFn()

    def test_empty_accumulator(self):
        acc = self.fn.create_accumulator()

        assert acc["event_count"] == 0
        assert acc["total_value_usd"] == 0.0
        assert acc["sessions"] == set()

    def test_add_input_counts_and_sums(self):
        acc = self.fn.create_accumulator()
        acc = self.fn.add_input(acc, make_event(value_usd=10.5, session_id="s1"))
        acc = self.fn.add_input(acc, make_event(value_usd=4.5, session_id="s2"))

        assert acc["event_count"] == 2
        assert acc["total_value_usd"] == 15.0
        assert acc["sessions"] == {"s1", "s2"}

    def test_none_value_usd_is_not_summed(self):
        acc = self.fn.create_accumulator()
        acc = self.fn.add_input(acc, make_event(value_usd=None))

        assert acc["event_count"] == 1
        assert acc["total_value_usd"] == 0.0

    def test_duplicate_sessions_counted_once(self):
        acc = self.fn.create_accumulator()
        for _ in range(3):
            acc = self.fn.add_input(acc, make_event(session_id="s1"))

        assert self.fn.extract_output(acc)["unique_sessions"] == 1

    def test_merge_accumulators(self):
        a = self.fn.create_accumulator()
        a = self.fn.add_input(a, make_event(value_usd=1.0, session_id="s1"))
        b = self.fn.create_accumulator()
        b = self.fn.add_input(b, make_event(value_usd=2.0, session_id="s2"))
        b = self.fn.add_input(b, make_event(value_usd=None, session_id="s1"))

        merged = self.fn.merge_accumulators([a, b])

        assert merged["event_count"] == 3
        assert merged["total_value_usd"] == 3.0
        assert merged["sessions"] == {"s1", "s2"}

    def test_extract_output_shape(self):
        acc = self.fn.create_accumulator()
        acc = self.fn.add_input(acc, make_event(value_usd=9.99, session_id="s1"))

        out = self.fn.extract_output(acc)

        assert out == {
            "event_count": 1,
            "total_value_usd": 9.99,
            "unique_sessions": 1,
        }

    def test_zero_revenue_window_reports_null_not_zero(self):
        """Documents the `or None` coercion: a window with events but no
        purchase value writes NULL to total_value_usd, not 0.0 — dashboards
        distinguish 'no revenue signal' from '$0.00 revenue'."""
        acc = self.fn.create_accumulator()
        acc = self.fn.add_input(acc, make_event(value_usd=None))

        assert self.fn.extract_output(acc)["total_value_usd"] is None


# ─────────────────────────────────────────────────────────────────────────────
# FormatAggregateRowDo
# ─────────────────────────────────────────────────────────────────────────────

class FakeWindow:
    """Stand-in for IntervalWindow — start/end as epoch seconds."""

    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end


class TestFormatAggregateRow:
    def test_bq_row_shape(self):
        element = (
            ("user-1", "purchase"),
            {"event_count": 5, "total_value_usd": 42.0, "unique_sessions": 3},
        )
        # 2025-06-15 15:06:00 UTC → 15:07:00 UTC (1-minute tumbling window)
        win = FakeWindow(1_750_000_000 - 40, 1_750_000_000 + 20)

        outputs = list(FormatAggregateRowDo().process(element, window=win))

        assert len(outputs) == 1
        row = outputs[0]
        assert row["user_id"] == "user-1"
        assert row["event_type"] == "purchase"
        assert row["window_start"] == "2025-06-15 15:06:00 UTC"
        assert row["window_end"] == "2025-06-15 15:07:00 UTC"
        assert row["window_date"] == "2025-06-15"
        assert row["event_count"] == 5
        assert row["total_value_usd"] == 42.0
        assert row["unique_sessions"] == 3
        assert row["computed_at"].endswith("UTC")
