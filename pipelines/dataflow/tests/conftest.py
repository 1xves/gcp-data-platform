"""
conftest.py — pytest configuration for Dataflow transform tests.

The transforms use flat imports (`from schemas import ...`) because the
Dataflow Flex Template launches with pipelines/dataflow/ as the working
directory. Tests replicate that by putting pipelines/dataflow/ on sys.path.

These tests invoke DoFns directly (process() / on_timer methods) rather than
running a full DirectRunner pipeline. Rationale:
  - Deterministic: no reliance on DirectRunner's partial support for
    processing-time timers.
  - Fast: no pipeline construction overhead per test case.
  - Beam metrics counters degrade to no-ops outside a pipeline context,
    so metric calls inside process() are safe.
"""

import sys
from pathlib import Path

# pipelines/dataflow/ — parent of this tests/ directory
DATAFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(DATAFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(DATAFLOW_ROOT))
