"""
test_dedup.py — unit tests for transforms/dedup.py

Covers:
  - StatefulDeduplicationDo: unique-vs-duplicate tagging, BagState mutation,
    GC timer arming, per-key state isolation, GC timer state clearing
  - build_dedup_key: user_id preference, session_id fallback, hard failure
    when neither is present (documents current contract)
  - PrepareDeduplicationKeyDo: KV wrapping

DoFns are invoked directly with fake state/timer objects — see conftest.py
for why this is preferred over a DirectRunner pipeline for stateful DoFns.
"""

import time

import pytest
from apache_beam import pvalue

from transforms.dedup import (
    DUPLICATE_TAG,
    GC_WINDOW_DURATION_SEC,
    UNIQUE_TAG,
    PrepareDeduplicationKeyDo,
    StatefulDeduplicationDo,
    build_dedup_key,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fakes for Beam state / timer parameters
# ─────────────────────────────────────────────────────────────────────────────

class FakeBagState:
    """Minimal stand-in for Beam BagState: read / add / clear."""

    def __init__(self, initial=None):
        self._items = list(initial or [])

    def read(self):
        return list(self._items)

    def add(self, value):
        self._items.append(value)

    def clear(self):
        self._items.clear()


class FakeTimer:
    """Records set() calls so tests can assert on timer arming."""

    def __init__(self):
        self.set_calls = []

    def set(self, timestamp):
        self.set_calls.append(timestamp)


def run_process(dofn, element, state, timer):
    """Invoke process() and return the list of emitted TaggedOutputs."""
    return list(dofn.process(element, seen_events_state=state, gc_timer=timer))


def make_event(event_id="evt-1", user_id="user-1", session_id="sess-1"):
    return {"event_id": event_id, "user_id": user_id, "session_id": session_id}


# ─────────────────────────────────────────────────────────────────────────────
# StatefulDeduplicationDo
# ─────────────────────────────────────────────────────────────────────────────

class TestStatefulDeduplication:
    def test_first_occurrence_is_unique(self):
        dofn = StatefulDeduplicationDo()
        state, timer = FakeBagState(), FakeTimer()
        event = make_event()

        outputs = run_process(dofn, ("user-1", event), state, timer)

        assert len(outputs) == 1
        assert isinstance(outputs[0], pvalue.TaggedOutput)
        assert outputs[0].tag == UNIQUE_TAG
        assert outputs[0].value == event

    def test_first_occurrence_marks_event_seen(self):
        dofn = StatefulDeduplicationDo()
        state, timer = FakeBagState(), FakeTimer()

        run_process(dofn, ("user-1", make_event(event_id="evt-42")), state, timer)

        assert state.read() == ["evt-42"]

    def test_first_occurrence_arms_gc_timer_24h_out(self):
        dofn = StatefulDeduplicationDo()
        state, timer = FakeBagState(), FakeTimer()

        before = time.time()
        run_process(dofn, ("user-1", make_event()), state, timer)
        after = time.time()

        assert len(timer.set_calls) == 1
        fire_at = timer.set_calls[0]
        assert before + GC_WINDOW_DURATION_SEC <= fire_at <= after + GC_WINDOW_DURATION_SEC

    def test_duplicate_is_tagged_and_dropped_from_unique_stream(self):
        dofn = StatefulDeduplicationDo()
        state = FakeBagState(initial=["evt-1"])
        timer = FakeTimer()
        event = make_event(event_id="evt-1")

        outputs = run_process(dofn, ("user-1", event), state, timer)

        assert len(outputs) == 1
        assert outputs[0].tag == DUPLICATE_TAG
        assert outputs[0].value == event

    def test_duplicate_does_not_mutate_state_or_rearm_timer(self):
        dofn = StatefulDeduplicationDo()
        state = FakeBagState(initial=["evt-1"])
        timer = FakeTimer()

        run_process(dofn, ("user-1", make_event(event_id="evt-1")), state, timer)

        assert state.read() == ["evt-1"]      # no double-add
        assert timer.set_calls == []          # timer only armed on first sight

    def test_distinct_event_ids_for_same_key_all_pass(self):
        dofn = StatefulDeduplicationDo()
        state, timer = FakeBagState(), FakeTimer()

        tags = []
        for i in range(3):
            outputs = run_process(
                dofn, ("user-1", make_event(event_id=f"evt-{i}")), state, timer
            )
            tags.append(outputs[0].tag)

        assert tags == [UNIQUE_TAG, UNIQUE_TAG, UNIQUE_TAG]
        assert sorted(state.read()) == ["evt-0", "evt-1", "evt-2"]

    def test_dedup_scope_is_per_key(self):
        """Same event_id under different keys (separate state) both pass —
        dedup is scoped per user, not global."""
        dofn = StatefulDeduplicationDo()
        timer = FakeTimer()
        state_a, state_b = FakeBagState(), FakeBagState()
        event = make_event(event_id="evt-shared")

        out_a = run_process(dofn, ("user-a", event), state_a, timer)
        out_b = run_process(dofn, ("user-b", event), state_b, timer)

        assert out_a[0].tag == UNIQUE_TAG
        assert out_b[0].tag == UNIQUE_TAG

    def test_gc_timer_clears_state(self):
        dofn = StatefulDeduplicationDo()
        state = FakeBagState(initial=["evt-1", "evt-2"])

        list(dofn.on_gc_timer(seen_events_state=state) or [])

        assert state.read() == []

    def test_event_reappears_as_unique_after_gc(self):
        """After the 24h GC window clears state, a previously-seen event_id is
        treated as unique again — documents the at-least-once limitation."""
        dofn = StatefulDeduplicationDo()
        state, timer = FakeBagState(), FakeTimer()
        event = make_event(event_id="evt-1")

        run_process(dofn, ("user-1", event), state, timer)
        list(dofn.on_gc_timer(seen_events_state=state) or [])
        outputs = run_process(dofn, ("user-1", event), state, timer)

        assert outputs[0].tag == UNIQUE_TAG


# ─────────────────────────────────────────────────────────────────────────────
# build_dedup_key
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildDedupKey:
    def test_prefers_user_id(self):
        assert build_dedup_key({"user_id": "u1", "session_id": "s1"}) == "u1"

    def test_falls_back_to_session_id_when_user_id_none(self):
        assert build_dedup_key({"user_id": None, "session_id": "s1"}) == "s1"

    def test_falls_back_to_session_id_when_user_id_absent(self):
        assert build_dedup_key({"session_id": "s1"}) == "s1"

    def test_falls_back_to_session_id_when_user_id_empty(self):
        # Empty string is falsy — anonymous users key on session.
        assert build_dedup_key({"user_id": "", "session_id": "s1"}) == "s1"

    def test_raises_when_no_user_or_session(self):
        # Contract: session_id is schema-required upstream (ValidateSchemaDo),
        # so a missing session_id here is a programming error, not bad data.
        with pytest.raises(KeyError):
            build_dedup_key({"user_id": None})


# ─────────────────────────────────────────────────────────────────────────────
# PrepareDeduplicationKeyDo
# ─────────────────────────────────────────────────────────────────────────────

class TestPrepareDeduplicationKey:
    def test_wraps_event_as_kv_pair(self):
        event = make_event(user_id="u9")
        outputs = list(PrepareDeduplicationKeyDo().process(event))

        assert outputs == [("u9", event)]

    def test_anonymous_event_keys_on_session(self):
        event = make_event(user_id=None, session_id="sess-77")
        outputs = list(PrepareDeduplicationKeyDo().process(event))

        assert outputs == [("sess-77", event)]
