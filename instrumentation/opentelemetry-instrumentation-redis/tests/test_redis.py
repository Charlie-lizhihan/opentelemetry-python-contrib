# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import logging
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest
import redis
import redis.asyncio
from redis.exceptions import WatchError

from opentelemetry import trace
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.semconv.trace import (
    DbSystemValues,
    NetTransportValues,
    SpanAttributes,
)
from opentelemetry.test.test_base import TestBase
from opentelemetry.trace import SpanKind, StatusCode


class TestRedis(TestBase):
    def setUp(self):
        super().setUp()
        RedisInstrumentor().instrument(tracer_provider=self.tracer_provider)

    def tearDown(self):
        super().tearDown()
        RedisInstrumentor().uninstrument()

    def test_span_properties(self):
        redis_client = redis.Redis()

        with mock.patch.object(redis_client, "connection"):
            redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.name, "GET")
        self.assertEqual(span.kind, SpanKind.CLIENT)

    def test_not_recording(self):
        redis_client = redis.Redis()

        mock_tracer = mock.Mock()
        mock_span = mock.Mock()
        mock_span.is_recording.return_value = False
        mock_tracer.start_span.return_value = mock_span
        with mock.patch("opentelemetry.trace.get_tracer") as tracer:
            with mock.patch.object(redis_client, "connection"):
                tracer.return_value = mock_tracer
                redis_client.get("key")
                self.assertFalse(mock_span.is_recording())
                self.assertTrue(mock_span.is_recording.called)
                self.assertFalse(mock_span.set_attribute.called)
                self.assertFalse(mock_span.set_status.called)

    def test_instrument_uninstrument(self):
        redis_client = redis.Redis()

        with mock.patch.object(redis_client, "connection"):
            redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.memory_exporter.clear()

        # Test uninstrument
        RedisInstrumentor().uninstrument()

        with mock.patch.object(redis_client, "connection"):
            redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)
        self.memory_exporter.clear()

        # Test instrument again
        RedisInstrumentor().instrument()

        with mock.patch.object(redis_client, "connection"):
            redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

    def test_instrument_uninstrument_async_client_command(self):
        redis_client = redis.asyncio.Redis()

        with mock.patch.object(redis_client, "connection", AsyncMock()):
            asyncio.run(redis_client.get("key"))

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.memory_exporter.clear()

        # Test uninstrument
        RedisInstrumentor().uninstrument()

        with mock.patch.object(redis_client, "connection", AsyncMock()):
            asyncio.run(redis_client.get("key"))

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)
        self.memory_exporter.clear()

        # Test instrument again
        RedisInstrumentor().instrument()

        with mock.patch.object(redis_client, "connection", AsyncMock()):
            asyncio.run(redis_client.get("key"))

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

    def test_response_hook(self):
        redis_client = redis.Redis()
        connection = redis.connection.Connection()
        redis_client.connection = connection

        response_attribute_name = "db.redis.response"

        def response_hook(span, conn, response):
            span.set_attribute(response_attribute_name, response)

        RedisInstrumentor().uninstrument()
        RedisInstrumentor().instrument(
            tracer_provider=self.tracer_provider, response_hook=response_hook
        )

        test_value = "test_value"

        with mock.patch.object(connection, "send_command"):
            with mock.patch.object(
                    redis_client, "parse_response", return_value=test_value
            ):
                redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(
            span.attributes.get(response_attribute_name), test_value
        )

    def test_request_hook(self):
        redis_client = redis.Redis()
        connection = redis.connection.Connection()
        redis_client.connection = connection

        custom_attribute_name = "my.request.attribute"

        def request_hook(span, conn, args, kwargs):
            if span and span.is_recording():
                span.set_attribute(custom_attribute_name, args[0])

        RedisInstrumentor().uninstrument()
        RedisInstrumentor().instrument(
            tracer_provider=self.tracer_provider, request_hook=request_hook
        )

        test_value = "test_value"

        with mock.patch.object(connection, "send_command"):
            with mock.patch.object(
                    redis_client, "parse_response", return_value=test_value
            ):
                redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(span.attributes.get(custom_attribute_name), "GET")

    def test_query_sanitizer_enabled(self):
        redis_client = redis.Redis()
        connection = redis.connection.Connection()
        redis_client.connection = connection

        RedisInstrumentor().uninstrument()
        RedisInstrumentor().instrument(
            tracer_provider=self.tracer_provider,
            sanitize_query=True,
        )

        with mock.patch.object(redis_client, "connection"):
            redis_client.set("key", "value")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(span.attributes.get("db.statement"), "SET ? ?")

    def test_query_sanitizer(self):
        redis_client = redis.Redis()
        connection = redis.connection.Connection()
        redis_client.connection = connection

        with mock.patch.object(redis_client, "connection"):
            redis_client.set("key", "value")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(span.attributes.get("db.statement"), "SET ? ?")

    def test_no_op_tracer_provider(self):
        RedisInstrumentor().uninstrument()
        tracer_provider = trace.NoOpTracerProvider()
        RedisInstrumentor().instrument(tracer_provider=tracer_provider)

        redis_client = redis.Redis()

        with mock.patch.object(redis_client, "connection"):
            redis_client.get("key")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)

    def test_attributes_default(self):
        redis_client = redis.Redis()

        with mock.patch.object(redis_client, "connection"):
            redis_client.set("key", "value")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(
            span.attributes[SpanAttributes.DB_SYSTEM],
            DbSystemValues.REDIS.value,
        )
        self.assertEqual(
            span.attributes[SpanAttributes.DB_REDIS_DATABASE_INDEX], 0
        )
        self.assertEqual(
            span.attributes[SpanAttributes.NET_PEER_NAME], "localhost"
        )
        self.assertEqual(span.attributes[SpanAttributes.NET_PEER_PORT], 6379)
        self.assertEqual(
            span.attributes[SpanAttributes.NET_TRANSPORT],
            NetTransportValues.IP_TCP.value,
        )

    def test_attributes_tcp(self):
        redis_client = redis.Redis.from_url("redis://foo:bar@1.1.1.1:6380/1")

        with mock.patch.object(redis_client, "connection"):
            redis_client.set("key", "value")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(
            span.attributes[SpanAttributes.DB_SYSTEM],
            DbSystemValues.REDIS.value,
        )
        self.assertEqual(
            span.attributes[SpanAttributes.DB_REDIS_DATABASE_INDEX], 1
        )
        self.assertEqual(
            span.attributes[SpanAttributes.NET_PEER_NAME], "1.1.1.1"
        )
        self.assertEqual(span.attributes[SpanAttributes.NET_PEER_PORT], 6380)
        self.assertEqual(
            span.attributes[SpanAttributes.NET_TRANSPORT],
            NetTransportValues.IP_TCP.value,
        )

    def test_attributes_unix_socket(self):
        redis_client = redis.Redis.from_url(
            "unix://foo@/path/to/socket.sock?db=3&password=bar"
        )

        with mock.patch.object(redis_client, "connection"):
            redis_client.set("key", "value")

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        span = spans[0]
        self.assertEqual(
            span.attributes[SpanAttributes.DB_SYSTEM],
            DbSystemValues.REDIS.value,
        )
        self.assertEqual(
            span.attributes[SpanAttributes.DB_REDIS_DATABASE_INDEX], 3
        )
        self.assertEqual(
            span.attributes[SpanAttributes.NET_PEER_NAME],
            "/path/to/socket.sock",
        )
        self.assertEqual(
            span.attributes[SpanAttributes.NET_TRANSPORT],
            NetTransportValues.OTHER.value,
        )

    def test_successful_transaction(self):
        redis_client = redis.Redis()

        # Create a mock pipeline
        mock_pipeline = mock.MagicMock()
        mock_pipeline.__enter__.return_value = mock_pipeline  # Ensure __enter__ returns the mock_pipeline
        mock_pipeline.watch.return_value = None
        mock_pipeline.multi.return_value = mock_pipeline
        mock_pipeline.execute.return_value = ["OK"]  # This is what we want to return

        with mock.patch.object(redis_client, "pipeline", return_value=mock_pipeline):
            with redis_client.pipeline() as pipe:
                pipe.watch("key")
                pipe.multi()
                pipe.set("key", "value")
                result = pipe.execute()

        # Check that the transaction was successful
        print(f"Result: {result}")
        self.assertEqual(result, ["OK"])

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]

        # Check that the span is not marked as an error
        self.assertIsNone(span.status.status_code)

        # Check that there are no exception events
        events = span.events
        self.assertEqual(len(events), 0)

        # Verify other span properties
        self.assertEqual(span.name, "MULTI")
        self.assertEqual(span.kind, SpanKind.CLIENT)
        self.assertEqual(span.attributes.get("db.system"), "redis")

        # Verify that the SET command is recorded in the span
        self.assertIn("SET", span.attributes.get("db.statement", ""))

        # Optionally, check for any additional attributes specific to your instrumentation
        # For example, you might want to verify that the database index is correctly recorded
        self.assertEqual(span.attributes.get("db.redis.database_index"), 0)

    def test_watch_error(self):
        redis_client = redis.Redis()

        # Mock the pipeline to raise a WatchError
        mock_pipeline = mock.MagicMock()
        mock_pipeline.watch.return_value = None
        mock_pipeline.multi.return_value = mock_pipeline
        mock_pipeline.execute.side_effect = WatchError("Watched variable changed")

        with mock.patch.object(redis_client, "pipeline", return_value=mock_pipeline):
            try:
                with redis_client.pipeline() as pipe:
                    pipe.watch("key")
                    pipe.multi()
                    pipe.set("key", "value")
                    pipe.execute()
            except WatchError:
                pass  # We expect this exception to be raised

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]

        # Check that the span is not marked as an error
        self.assertIsNone(span.status.status_code)

        # Check that the WatchError is recorded as an event, not an exception
        events = span.events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "exception")
        self.assertEqual(events[0].attributes["exception.type"], "WatchError")
        self.assertIn("Watched variable changed", events[0].attributes["exception.message"])

        # Verify other span properties
        self.assertEqual(span.name, "MULTI")
        self.assertEqual(span.kind, SpanKind.CLIENT)
        self.assertEqual(span.attributes.get("db.system"), "redis")


import pytest
import redis.asyncio
from redis.exceptions import WatchError
from opentelemetry import trace
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import \
    InMemorySpanExporter  # This is the correct import for MemorySpanExporter


class Test_Redis:
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        # Setup
        self.tracer_provider = TracerProvider()
        self.memory_exporter = InMemorySpanExporter()
        span_processor = SimpleSpanProcessor(self.memory_exporter)
        self.tracer_provider.add_span_processor(span_processor)
        trace.set_tracer_provider(self.tracer_provider)

        RedisInstrumentor().instrument(tracer_provider=self.tracer_provider)

        yield

        # Teardown
        RedisInstrumentor().uninstrument()

    @pytest.mark.asyncio
    async def test_watch_error(self):
        r = redis.asyncio.Redis()
        await r.set("a", "0")

        try:
            async with r.pipeline(transaction=False) as pipe:
                await pipe.watch("a")
                a = await pipe.get("a")

                # Simulate a change by another client
                await r.set("a", "bad")

                pipe.multi()
                await pipe.set("a", str(int(a) + 1))

                await pipe.execute()
        except WatchError:
            print("WatchError caught as expected")
        else:
            pytest.fail("WatchError was not raised")

        spans = self.memory_exporter.get_finished_spans()
        assert len(spans) > 0, "No spans were recorded"

        # Check the last span for WatchError evidence
        last_span = spans[-1]

        # The span itself should not be marked as an error
        assert last_span.status.status_code is None

        # Check for WatchError in span events
        watch_error_events = [event for event in last_span.events
                              if event.name == "exception" and
                              event.attributes.get("exception.type") == "WatchError"]
        assert len(watch_error_events) > 0, "WatchError event not found in span"

        # Verify that the value in Redis wasn't changed due to the WatchError
        final_value = await r.get("a")
        assert final_value == b"bad"

        # Clean up
        await r.delete("a")