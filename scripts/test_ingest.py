import json
import time
import uuid
from google.cloud import pubsub_v1
import avro.schema
import avro.io
import io

PROJECT_ID = "project-6db0f664-1423-47cb-86d"
TOPIC_ID = "stg-events"

schema_str = """
{
  "type": "record",
  "name": "PlatformEvent",
  "namespace": "com.platform.events.v1",
  "fields": [
    {"name": "event_id", "type": "string"},
    {"name": "event_type", "type": {"type": "enum", "name": "EventType", "symbols": ["page_view", "click", "purchase", "add_to_cart", "search", "error", "session_start", "session_end"]}},
    {"name": "user_id", "type": ["null", "string"], "default": null},
    {"name": "session_id", "type": "string"},
    {"name": "timestamp_ms", "type": "long"},
    {"name": "properties", "type": {"type": "map", "values": "string"}, "default": {}},
    {"name": "schema_version", "type": "int", "default": 1},
    {"name": "producer_id", "type": "string"},
    {"name": "environment", "type": {"type": "enum", "name": "Environment", "symbols": ["production", "staging", "development"]}, "default": "production"}
  ]
}
"""

def encode_avro(data, schema):
    writer = avro.io.DatumWriter(schema)
    bytes_io = io.BytesIO()
    encoder = avro.io.BinaryEncoder(bytes_io)
    writer.write(data, encoder)
    return bytes_io.getvalue()

schema = avro.schema.parse(schema_str)
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_ID)

event_data = {
    "event_id": str(uuid.uuid4()),
    "event_type": "page_view",
    "user_id": "user_999",
    "session_id": "sess_123",
    "timestamp_ms": int(time.time() * 1000),
    "properties": {"page": "/home", "cli_test": "true"},
    "schema_version": 1,
    "producer_id": "cli-generator",
    "environment": "staging"
}

print(f"Sending event for user {event_data['user_id']}...")
avro_bytes = encode_avro(event_data, schema)
future = publisher.publish(topic_path, avro_bytes)
print(f"Published message ID: {future.result()}")
