"""
transforms/aggregate.py — Windowing and aggregation transforms.

Window strategy:
- Tumbling 1-minute windows per (user_id, event_type)
- These aggregates power real-time dashboards and are ingested into the Feature Store daily
- Allowed lateness: 30 seconds (handles Pub/Sub delivery delay; late data updates the window)
- Accumulation mode: ACCUMULATING (late data updates existing output, not appends a new one)

Output goes to BigQuery event_aggregates table — not to the raw sink.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Tuple

import apache_beam as beam
from apache_beam import window
from apache_beam.transforms.trigger import (
    AccumulationMode,
    AfterCount,
    AfterWatermark,
    Repeatedly,
)


class ExtractWindowKeyDo(beam.DoFn):
    """Extract (user_id, event_type) as the aggregation key."""

    def process(self, event: Dict[str, Any], *args, **kwargs):
        user_id    = event.get("user_id") or event["session_id"]
        event_type = event["event_type"]
        yield ((user_id, event_type), event)


class EventAggregationFn(beam.CombineFn):
    """
    Custom CombineFn to aggregate events within a tumbling window.

    Accumulates:
        - event_count (total events in window)
        - total_value_usd (sum of purchase values)
        - unique_sessions (set of distinct session_ids)
    """

    def create_accumulator(self) -> Dict:
        return {
            "event_count":     0,
            "total_value_usd": 0.0,
            "sessions":        set(),
        }

    def add_input(self, accumulator: Dict, event: Dict) -> Dict:
        accumulator["event_count"] += 1
        value = event.get("value_usd")
        if value is not None:
            accumulator["total_value_usd"] += float(value)
        session = event.get("session_id")
        if session:
            accumulator["sessions"].add(session)
        return accumulator

    def merge_accumulators(self, accumulators) -> Dict:
        merged = self.create_accumulator()
        for acc in accumulators:
            merged["event_count"]     += acc["event_count"]
            merged["total_value_usd"] += acc["total_value_usd"]
            merged["sessions"]        |= acc["sessions"]
        return merged

    def extract_output(self, accumulator: Dict) -> Dict:
        return {
            "event_count":     accumulator["event_count"],
            "total_value_usd": accumulator["total_value_usd"] or None,
            "unique_sessions": len(accumulator["sessions"]),
        }


class FormatAggregateRowDo(beam.DoFn):
    """
    Convert (key, aggregate, window) into a BigQuery-ready row dict.
    Receives window metadata via the window parameter injection.
    """

    def process(
        self,
        element: Tuple[Tuple[str, str], Dict],
        window=beam.DoFn.WindowParam,
        *args,
        **kwargs,
    ):
        (user_id, event_type), agg = element
        now = datetime.now(timezone.utc)

        win_start = datetime.fromtimestamp(window.start, tz=timezone.utc)
        win_end   = datetime.fromtimestamp(window.end,   tz=timezone.utc)

        yield {
            "user_id":         user_id,
            "event_type":      event_type,
            "window_start":    win_start.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "window_end":      win_end.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "window_date":     win_start.strftime("%Y-%m-%d"),
            "event_count":     agg["event_count"],
            "total_value_usd": agg["total_value_usd"],
            "unique_sessions": agg["unique_sessions"],
            "computed_at":     now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }


def build_aggregation_pipeline(events_pcoll, agg_output_table: str):
    """
    Compose the full windowed aggregation pipeline.

    Args:
        events_pcoll: PCollection of enriched event dicts
        agg_output_table: BigQuery table spec (project:dataset.table)

    Returns:
        PCollection writing results to BigQuery (side-effect pipeline)
    """
    from schemas import EVENT_AGGREGATES_BQ_SCHEMA
    from apache_beam.io import BigQueryDisposition

    return (
        events_pcoll
        | "ExtractWindowKey" >> beam.ParDo(ExtractWindowKeyDo())
        | "TumblingWindow1m" >> beam.WindowInto(
            window.FixedWindows(60),  # 1-minute tumbling windows
            trigger=AfterWatermark(
                late=Repeatedly(AfterCount(1))  # Emit updated agg for each late element
            ),
            accumulation_mode=AccumulationMode.ACCUMULATING,
            allowed_lateness=30,  # 30s lateness tolerance
        )
        | "AggregatePerWindow" >> beam.CombinePerKey(EventAggregationFn())
        | "FormatAggRow"       >> beam.ParDo(FormatAggregateRowDo())
        | "WriteAggBQ"         >> beam.io.WriteToBigQuery(
            table=agg_output_table,
            schema=EVENT_AGGREGATES_BQ_SCHEMA,
            write_disposition=BigQueryDisposition.WRITE_APPEND,
            create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
            # FILE_LOADS for high-throughput windows; STREAMING_INSERTS for low-latency
            method=beam.io.WriteToBigQuery.Method.FILE_LOADS,
            triggering_frequency=60,  # Flush to BQ every 60 seconds
        )
    )
