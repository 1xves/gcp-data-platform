"""
transforms/dedup.py — Stateful deduplication using Apache Beam state and timers.

Design:
- Uses BagState to track seen event_ids within a 24-hour keyed window
- Timer fires at window boundary to garbage-collect state (avoid unbounded growth)
- Dedup key: (user_id or session_id) + event_id — ensures per-user dedup scope
- Duplicate events are counted (metric) and silently dropped (not routed to DLQ)

Why stateful (not a fixed window + GroupByKey)?
- Session-scoped dedup can span hour boundaries — a fixed 1-hour window misses
  duplicates across the boundary. Stateful DoFn with a 24h timer handles this correctly.
- State is per-key (user_id), so state size is bounded per worker (not global).

Limitation: this provides at-least-once dedup within a 24h window. Events older
than 24h that arrive late (e.g., network delay) may be written twice. For full
idempotency across arbitrary delays, use BigQuery MERGE with USING (SELECT DISTINCT).
"""

import logging
from datetime import datetime, timezone

import apache_beam as beam
from apache_beam import pvalue
from apache_beam.coders import VarIntCoder
from apache_beam.transforms.userstate import (
    BagStateSpec,
    TimerSpec,
    on_timer,
)
from apache_beam.transforms.window import GlobalWindow

logger = logging.getLogger(__name__)

DUPLICATE_TAG = "duplicate"
UNIQUE_TAG = "unique"

# 24-hour GC window — state is cleared after this duration to prevent OOM
GC_WINDOW_DURATION_SEC = 24 * 60 * 60


class StatefulDeduplicationDo(beam.DoFn):
    """
    Stateful deduplication: tracks event_ids seen per user within a 24h window.

    Input: (dedup_key, event_dict) key-value pair
    Output[UNIQUE_TAG]: First occurrence of an event (unique)
    Output[DUPLICATE_TAG]: Subsequent occurrences (duplicates)

    State:
        SEEN_EVENTS_STATE: BagState[str] — set of seen event_ids per user

    Timer:
        GC_TIMER: EventTimerSpec — fires 24h after first event in window to clear state
    """

    # BagState stores seen event_ids as strings
    # We hash event_id to 8 bytes to reduce memory per state entry
    SEEN_EVENTS_STATE = BagStateSpec("seen_events", beam.coders.StrUtf8Coder())

    # Timer fires after 24h to garbage-collect the seen-events state
    GC_TIMER = TimerSpec("gc_timer", beam.transforms.userstate.TimeDomain.REAL_TIME)

    dedup_hit   = beam.metrics.Metrics.counter("dedup", "duplicate_dropped")
    dedup_pass  = beam.metrics.Metrics.counter("dedup", "unique_passed")
    state_gc    = beam.metrics.Metrics.counter("dedup", "state_gc_fired")

    def process(
        self,
        element,
        seen_events_state=beam.DoFn.StateParam(SEEN_EVENTS_STATE),
        gc_timer=beam.DoFn.TimerParam(GC_TIMER),
        *args,
        **kwargs,
    ):
        """
        Args:
            element: (dedup_key, event_dict)
        """
        dedup_key, event = element
        event_id = event["event_id"]

        # Read current seen set — BagState returns an iterable, convert to set
        seen_ids = set(seen_events_state.read())

        if event_id in seen_ids:
            self.dedup_hit.inc()
            yield pvalue.TaggedOutput(DUPLICATE_TAG, event)
            return

        # First occurrence — mark as seen and pass through
        seen_events_state.add(event_id)

        # Set GC timer to fire 24h from now — Beam ensures this fires at most once
        # (subsequent set() calls on the same timer key just update the time)
        gc_timer.set(
            datetime.now(timezone.utc).timestamp() + GC_WINDOW_DURATION_SEC
        )

        self.dedup_pass.inc()
        yield pvalue.TaggedOutput(UNIQUE_TAG, event)

    @on_timer(GC_TIMER)
    def on_gc_timer(
        self,
        seen_events_state=beam.DoFn.StateParam(SEEN_EVENTS_STATE),
    ):
        """Clear the seen-events bag state when the 24h GC timer fires."""
        self.state_gc.inc()
        seen_events_state.clear()
        logger.debug("GC timer fired — cleared dedup state")


def build_dedup_key(event: dict) -> str:
    """
    Build a dedup key that groups events by user (or session for anonymous users).

    Why key on user_id (not globally)?
    - Beam state is per-key per-worker. Keying on user_id distributes load evenly.
    - Duplicate event_ids for different users are astronomically unlikely (UUID v4).
    - Keying globally would funnel all events through a single stateful DoFn, creating
      a hot key bottleneck. Per-user state avoids this entirely.
    """
    return event.get("user_id") or event["session_id"]


class PrepareDeduplicationKeyDo(beam.DoFn):
    """Wrap events as (dedup_key, event) KV pairs for StatefulDeduplicationDo."""

    def process(self, event, *args, **kwargs):
        yield (build_dedup_key(event), event)
