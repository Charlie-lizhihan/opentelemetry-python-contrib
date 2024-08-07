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
import unittest
from timeit import default_timer
from unittest.mock import patch

import fastapi
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.testclient import TestClient

import opentelemetry.instrumentation.fastapi as otel_fastapi
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    NumberDataPoint,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.test.test_base import TestBase
from opentelemetry.util.http import (
    _active_requests_count_attrs,
    _duration_attrs,
    get_excluded_urls,
)

_expected_metric_names = [
    "http.server.active_requests",
    "http.server.duration",
    "http.server.response.size",
    "http.server.request.size",
]
_recommended_attrs = {
    "http.server.active_requests": _active_requests_count_attrs,
    "http.server.duration": {*_duration_attrs, SpanAttributes.HTTP_TARGET},
    "http.server.response.size": {
        *_duration_attrs,
        SpanAttributes.HTTP_TARGET,
    },
    "http.server.request.size": {
        *_duration_attrs,
        SpanAttributes.HTTP_TARGET,
    },
}


class TestBaseFastAPI(TestBase):
    def _create_app(self):
        app = self._create_fastapi_app()
        self._instrumentor.instrument_app(
            app=app,
            server_request_hook=getattr(self, "server_request_hook", None),
            client_request_hook=getattr(self, "client_request_hook", None),
            client_response_hook=getattr(self, "client_response_hook", None),
        )
        return app

    def _create_app_explicit_excluded_urls(self):
        app = self._create_fastapi_app()
        to_exclude = "/user/123,/foobar"
        self._instrumentor.instrument_app(
            app,
            excluded_urls=to_exclude,
            server_request_hook=getattr(self, "server_request_hook", None),
            client_request_hook=getattr(self, "client_request_hook", None),
            client_response_hook=getattr(self, "client_response_hook", None),
        )
        return app

    @classmethod
    def setUpClass(cls):
        if cls is TestBaseFastAPI:
            raise unittest.SkipTest(
                f"{cls.__name__} is an abstract base class"
            )

        super(TestBaseFastAPI, cls).setUpClass()

    def setUp(self):
        super().setUp()
        self.env_patch = patch.dict(
            "os.environ",
            {"OTEL_PYTHON_FASTAPI_EXCLUDED_URLS": "/exclude/123,healthzz"},
        )
        self.env_patch.start()
        self.exclude_patch = patch(
            "opentelemetry.instrumentation.fastapi._excluded_urls_from_env",
            get_excluded_urls("FASTAPI"),
        )
        self.exclude_patch.start()
        self._instrumentor = otel_fastapi.FastAPIInstrumentor()
        self._app = self._create_app()
        self._app.add_middleware(HTTPSRedirectMiddleware)
        self._client = TestClient(self._app)

    def tearDown(self):
        super().tearDown()
        self.env_patch.stop()
        self.exclude_patch.stop()
        with self.disable_logging():
            self._instrumentor.uninstrument()
            self._instrumentor.uninstrument_app(self._app)

    @staticmethod
    def _create_fastapi_app():
        app = fastapi.FastAPI()
        sub_app = fastapi.FastAPI()

        @sub_app.get("/home")
        async def _():
            return {"message": "sub hi"}

        @app.get("/foobar")
        async def _():
            return {"message": "hello world"}

        @app.get("/user/{username}")
        async def _(username: str):
            return {"message": username}

        @app.get("/exclude/{param}")
        async def _(param: str):
            return {"message": param}

        @app.get("/healthzz")
        async def _():
            return {"message": "ok"}

        app.mount("/sub", app=sub_app)

        return app


class TestBaseManualFastAPI(TestBaseFastAPI):

    @classmethod
    def setUpClass(cls):
        if cls is TestBaseManualFastAPI:
            raise unittest.SkipTest(
                f"{cls.__name__} is an abstract base class"
            )

        super(TestBaseManualFastAPI, cls).setUpClass()

    def test_sub_app_fastapi_call(self):
        """
        This test is to ensure that a span in case of a sub app targeted contains the correct server url

        As this test case covers manual instrumentation, we won't see any additional spans for the sub app.
        In this case all generated spans might suffice the requirements for the attributes already
        (as the testcase is not setting a root_path for the outer app here)
        """

        self._client.get("/sub/home")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        for span in spans:
            # As we are only looking to the "outer" app, we would see only the "GET /sub" spans
            self.assertIn("GET /sub", span.name)

        # We now want to specifically test all spans including the
        # - HTTP_TARGET
        # - HTTP_URL
        # attributes to be populated with the expected values
        spans_with_http_attributes = [
            span
            for span in spans
            if (
                SpanAttributes.HTTP_URL in span.attributes
                or SpanAttributes.HTTP_TARGET in span.attributes
            )
        ]

        # We expect only one span to have the HTTP attributes set (the SERVER span from the app itself)
        # the sub app is not instrumented with manual instrumentation tests.
        self.assertEqual(1, len(spans_with_http_attributes))

        for span in spans_with_http_attributes:
            self.assertEqual(
                "/sub/home", span.attributes[SpanAttributes.HTTP_TARGET]
            )
        self.assertEqual(
            "https://testserver:443/sub/home",
            span.attributes[SpanAttributes.HTTP_URL],
        )


class TestBaseAutoFastAPI(TestBaseFastAPI):

    @classmethod
    def setUpClass(cls):
        if cls is TestBaseAutoFastAPI:
            raise unittest.SkipTest(
                f"{cls.__name__} is an abstract base class"
            )

        super(TestBaseAutoFastAPI, cls).setUpClass()

    def test_sub_app_fastapi_call(self):
        """
        This test is to ensure that a span in case of a sub app targeted contains the correct server url

        As this test case covers auto instrumentation, we will see additional spans for the sub app.
        In this case all generated spans might suffice the requirements for the attributes already
        (as the testcase is not setting a root_path for the outer app here)
        """

        self._client.get("/sub/home")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 6)

        for span in spans:
            # As we are only looking to the "outer" app, we would see only the "GET /sub" spans
            #   -> the outer app is not aware of the sub_apps internal routes
            sub_in = "GET /sub" in span.name
            # The sub app spans are named GET /home as from the sub app perspective the request targets /home
            #   -> the sub app is technically not aware of the /sub prefix
            home_in = "GET /home" in span.name

            # We expect the spans to be either from the outer app or the sub app
            self.assertTrue(
                sub_in or home_in,
                f"Span {span.name} does not have /sub or /home in its name",
            )

        # We now want to specifically test all spans including the
        # - HTTP_TARGET
        # - HTTP_URL
        # attributes to be populated with the expected values
        spans_with_http_attributes = [
            span
            for span in spans
            if (
                SpanAttributes.HTTP_URL in span.attributes
                or SpanAttributes.HTTP_TARGET in span.attributes
            )
        ]

        # We now expect spans with attributes from both the app and its sub app
        self.assertEqual(2, len(spans_with_http_attributes))

        for span in spans_with_http_attributes:
            self.assertEqual(
                "/sub/home", span.attributes[SpanAttributes.HTTP_TARGET]
            )
        self.assertEqual(
            "https://testserver:443/sub/home",
            span.attributes[SpanAttributes.HTTP_URL],
        )


class TestFastAPIManualInstrumentation(TestBaseManualFastAPI):
    def test_instrument_app_with_instrument(self):
        if not isinstance(self, TestAutoInstrumentation):
            self._instrumentor.instrument()
        self._client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        for span in spans:
            self.assertIn("GET /foobar", span.name)
            self.assertEqual(
                span.instrumentation_scope.name,
                "opentelemetry.instrumentation.fastapi",
            )

    def test_uninstrument_app(self):
        self._client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)

        self._instrumentor.uninstrument_app(self._app)
        self.assertFalse(
            isinstance(
                self._app.user_middleware[0].cls, OpenTelemetryMiddleware
            )
        )
        self._client = TestClient(self._app)
        resp = self._client.get("/foobar")
        self.assertEqual(200, resp.status_code)
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 3)

    def test_uninstrument_app_after_instrument(self):
        if not isinstance(self, TestAutoInstrumentation):
            self._instrumentor.instrument()
        self._instrumentor.uninstrument_app(self._app)
        self._client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)

    def test_basic_fastapi_call(self):
        self._client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        for span in spans:
            self.assertIn("GET /foobar", span.name)

    def test_fastapi_route_attribute_added(self):
        """Ensure that fastapi routes are used as the span name."""
        self._client.get("/user/123")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        for span in spans:
            self.assertIn("GET /user/{username}", span.name)
        self.assertEqual(
            spans[-1].attributes[SpanAttributes.HTTP_ROUTE], "/user/{username}"
        )
        # ensure that at least one attribute that is populated by
        # the asgi instrumentation is successfully feeding though.
        self.assertEqual(
            spans[-1].attributes[SpanAttributes.HTTP_FLAVOR], "1.1"
        )

    def test_fastapi_excluded_urls(self):
        """Ensure that given fastapi routes are excluded."""
        self._client.get("/exclude/123")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)
        self._client.get("/healthzz")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)

    def test_fastapi_excluded_urls_not_env(self):
        """Ensure that given fastapi routes are excluded when passed explicitly (not in the environment)"""
        app = self._create_app_explicit_excluded_urls()
        client = TestClient(app)
        client.get("/user/123")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)
        client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)

    def test_fastapi_metrics(self):
        self._client.get("/foobar")
        self._client.get("/foobar")
        self._client.get("/foobar")
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        number_data_point_seen = False
        histogram_data_point_seen = False
        self.assertTrue(len(metrics_list.resource_metrics) == 1)
        for resource_metric in metrics_list.resource_metrics:
            self.assertTrue(len(resource_metric.scope_metrics) == 1)
            for scope_metric in resource_metric.scope_metrics:
                self.assertEqual(
                    scope_metric.scope.name,
                    "opentelemetry.instrumentation.fastapi",
                )
                self.assertTrue(len(scope_metric.metrics) == 3)
                for metric in scope_metric.metrics:
                    self.assertIn(metric.name, _expected_metric_names)
                    data_points = list(metric.data.data_points)
                    self.assertEqual(len(data_points), 1)
                    for point in data_points:
                        if isinstance(point, HistogramDataPoint):
                            self.assertEqual(point.count, 3)
                            histogram_data_point_seen = True
                        if isinstance(point, NumberDataPoint):
                            number_data_point_seen = True
                        for attr in point.attributes:
                            self.assertIn(
                                attr, _recommended_attrs[metric.name]
                            )
        self.assertTrue(number_data_point_seen and histogram_data_point_seen)

    def test_basic_metric_success(self):
        start = default_timer()
        self._client.get("/foobar")
        duration = max(round((default_timer() - start) * 1000), 0)
        expected_duration_attributes = {
            "http.method": "GET",
            "http.host": "testserver:443",
            "http.scheme": "https",
            "http.flavor": "1.1",
            "http.server_name": "testserver",
            "net.host.port": 443,
            "http.status_code": 200,
            "http.target": "/foobar",
        }
        expected_requests_count_attributes = {
            "http.method": "GET",
            "http.host": "testserver:443",
            "http.scheme": "https",
            "http.flavor": "1.1",
            "http.server_name": "testserver",
        }
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for metric in (
            metrics_list.resource_metrics[0].scope_metrics[0].metrics
        ):
            for point in list(metric.data.data_points):
                if isinstance(point, HistogramDataPoint):
                    self.assertDictEqual(
                        expected_duration_attributes,
                        dict(point.attributes),
                    )
                    self.assertEqual(point.count, 1)
                    self.assertAlmostEqual(duration, point.sum, delta=40)
                if isinstance(point, NumberDataPoint):
                    self.assertDictEqual(
                        expected_requests_count_attributes,
                        dict(point.attributes),
                    )
                    self.assertEqual(point.value, 0)

    def test_basic_post_request_metric_success(self):
        start = default_timer()
        response = self._client.post(
            "/foobar",
            json={"foo": "bar"},
        )
        duration = max(round((default_timer() - start) * 1000), 0)
        response_size = int(response.headers.get("content-length"))
        request_size = int(response.request.headers.get("content-length"))
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for metric in (
            metrics_list.resource_metrics[0].scope_metrics[0].metrics
        ):
            for point in list(metric.data.data_points):
                if isinstance(point, HistogramDataPoint):
                    self.assertEqual(point.count, 1)
                    if metric.name == "http.server.duration":
                        self.assertAlmostEqual(duration, point.sum, delta=40)
                    elif metric.name == "http.server.response.size":
                        self.assertEqual(response_size, point.sum)
                    elif metric.name == "http.server.request.size":
                        self.assertEqual(request_size, point.sum)
                if isinstance(point, NumberDataPoint):
                    self.assertEqual(point.value, 0)

    def test_metric_uninstrument_app(self):
        self._client.get("/foobar")
        self._instrumentor.uninstrument_app(self._app)
        self._client.get("/foobar")
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for metric in (
            metrics_list.resource_metrics[0].scope_metrics[0].metrics
        ):
            for point in list(metric.data.data_points):
                if isinstance(point, HistogramDataPoint):
                    self.assertEqual(point.count, 1)
                if isinstance(point, NumberDataPoint):
                    self.assertEqual(point.value, 0)

    def test_metric_uninstrument(self):
        if not isinstance(self, TestAutoInstrumentation):
            self._instrumentor.instrument()
        self._client.get("/foobar")
        self._instrumentor.uninstrument()
        self._client.get("/foobar")

        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for metric in (
            metrics_list.resource_metrics[0].scope_metrics[0].metrics
        ):
            for point in list(metric.data.data_points):
                if isinstance(point, HistogramDataPoint):
                    self.assertEqual(point.count, 1)
                if isinstance(point, NumberDataPoint):
                    self.assertEqual(point.value, 0)

    @staticmethod
    def _create_fastapi_app():
        app = fastapi.FastAPI()
        sub_app = fastapi.FastAPI()

        @sub_app.get("/home")
        async def _():
            return {"message": "sub hi"}

        @app.get("/foobar")
        async def _():
            return {"message": "hello world"}

        @app.get("/user/{username}")
        async def _(username: str):
            return {"message": username}

        @app.get("/exclude/{param}")
        async def _(param: str):
            return {"message": param}

        @app.get("/healthzz")
        async def _():
            return {"message": "ok"}

        app.mount("/sub", app=sub_app)

        return app


class TestFastAPIManualInstrumentationHooks(TestBaseManualFastAPI):
    _server_request_hook = None
    _client_request_hook = None
    _client_response_hook = None

    def server_request_hook(self, span, scope):
        if self._server_request_hook is not None:
            self._server_request_hook(span, scope)

    def client_request_hook(self, receive_span, scope, message):
        if self._client_request_hook is not None:
            self._client_request_hook(receive_span, scope, message)

    def client_response_hook(self, send_span, scope, message):
        if self._client_response_hook is not None:
            self._client_response_hook(send_span, scope, message)

    def test_hooks(self):
        def server_request_hook(span, scope):
            span.update_name("name from server hook")

        def client_request_hook(receive_span, scope, message):
            receive_span.update_name("name from client hook")
            receive_span.set_attribute("attr-from-request-hook", "set")

        def client_response_hook(send_span, scope, message):
            send_span.update_name("name from response hook")
            send_span.set_attribute("attr-from-response-hook", "value")

        self._server_request_hook = server_request_hook
        self._client_request_hook = client_request_hook
        self._client_response_hook = client_response_hook

        self._client.get("/foobar")
        spans = self.sorted_spans(self.memory_exporter.get_finished_spans())
        self.assertEqual(
            len(spans), 3
        )  # 1 server span and 2 response spans (response start and body)

        server_span = spans[2]
        self.assertEqual(server_span.name, "name from server hook")

        response_spans = spans[:2]
        for span in response_spans:
            self.assertEqual(span.name, "name from response hook")
            self.assertSpanHasAttributes(
                span, {"attr-from-response-hook": "value"}
            )


class TestAutoInstrumentation(TestBaseAutoFastAPI):
    """Test the auto-instrumented variant

    Extending the manual instrumentation as most test cases apply
    to both.
    """

    def _create_app(self):
        # instrumentation is handled by the instrument call
        resource = Resource.create({"key1": "value1", "key2": "value2"})
        result = self.create_tracer_provider(resource=resource)
        tracer_provider, exporter = result
        self.memory_exporter = exporter

        self._instrumentor.instrument(tracer_provider=tracer_provider)
        return self._create_fastapi_app()

    def _create_app_explicit_excluded_urls(self):
        resource = Resource.create({"key1": "value1", "key2": "value2"})
        tracer_provider, exporter = self.create_tracer_provider(
            resource=resource
        )
        self.memory_exporter = exporter

        to_exclude = "/user/123,/foobar"
        self._instrumentor.uninstrument()  # Disable previous instrumentation (setUp)
        self._instrumentor.instrument(
            tracer_provider=tracer_provider,
            excluded_urls=to_exclude,
        )
        return self._create_fastapi_app()

    def test_request(self):
        self._client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        for span in spans:
            self.assertEqual(span.resource.attributes["key1"], "value1")
            self.assertEqual(span.resource.attributes["key2"], "value2")

    def test_mulitple_way_instrumentation(self):
        self._instrumentor.instrument_app(self._app)
        count = 0
        for middleware in self._app.user_middleware:
            if middleware.cls is OpenTelemetryMiddleware:
                count += 1
        self.assertEqual(count, 1)

    def test_uninstrument_after_instrument(self):
        app = self._create_fastapi_app()
        client = TestClient(app)
        client.get("/foobar")
        self._instrumentor.uninstrument()
        client.get("/foobar")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)

    def tearDown(self):
        self._instrumentor.uninstrument()
        super().tearDown()

    def test_sub_app_fastapi_call(self):
        """
        !!! Attention: we need to override this testcase for the auto-instrumented variant
            The reason is, that with auto instrumentation, the sub app is instrumented as well
            and therefore we would see the spans for the sub app as well

        This test is to ensure that a span in case of a sub app targeted contains the correct server url
        """

        self._client.get("/sub/home")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 6)

        for span in spans:
            # As we are only looking to the "outer" app, we would see only the "GET /sub" spans
            #   -> the outer app is not aware of the sub_apps internal routes
            sub_in = "GET /sub" in span.name
            # The sub app spans are named GET /home as from the sub app perspective the request targets /home
            #   -> the sub app is technically not aware of the /sub prefix
            home_in = "GET /home" in span.name

            # We expect the spans to be either from the outer app or the sub app
            self.assertTrue(
                sub_in or home_in,
                f"Span {span.name} does not have /sub or /home in its name",
            )

        # We now want to specifically test all spans including the
        # - HTTP_TARGET
        # - HTTP_URL
        # attributes to be populated with the expected values
        spans_with_http_attributes = [
            span
            for span in spans
            if (
                SpanAttributes.HTTP_URL in span.attributes
                or SpanAttributes.HTTP_TARGET in span.attributes
            )
        ]

        # We now expect spans with attributes from both the app and its sub app
        self.assertEqual(2, len(spans_with_http_attributes))

        for span in spans_with_http_attributes:
            self.assertEqual(
                "/sub/home", span.attributes[SpanAttributes.HTTP_TARGET]
            )
        self.assertEqual(
            "https://testserver:443/sub/home",
            span.attributes[SpanAttributes.HTTP_URL],
        )


class TestAutoInstrumentationHooks(TestBaseAutoFastAPI):
    """
    Test the auto-instrumented variant for request and response hooks

    Extending the manual instrumentation to inherit defined hooks and since most test cases apply
    to both.
    """

    def _create_app(self):
        # instrumentation is handled by the instrument call
        self._instrumentor.instrument(
            server_request_hook=getattr(self, "server_request_hook", None),
            client_request_hook=getattr(self, "client_request_hook", None),
            client_response_hook=getattr(self, "client_response_hook", None),
        )

        return self._create_fastapi_app()

    def _create_app_explicit_excluded_urls(self):
        resource = Resource.create({"key1": "value1", "key2": "value2"})
        tracer_provider, exporter = self.create_tracer_provider(
            resource=resource
        )
        self.memory_exporter = exporter

        to_exclude = "/user/123,/foobar"
        self._instrumentor.uninstrument()  # Disable previous instrumentation (setUp)
        self._instrumentor.instrument(
            tracer_provider=tracer_provider,
            excluded_urls=to_exclude,
            server_request_hook=getattr(self, "server_request_hook", None),
            client_request_hook=getattr(self, "client_request_hook", None),
            client_response_hook=getattr(self, "client_response_hook", None),
        )
        return self._create_fastapi_app()

    def tearDown(self):
        self._instrumentor.uninstrument()
        super().tearDown()

    def test_sub_app_fastapi_call(self):
        """
        !!! Attention: we need to override this testcase for the auto-instrumented variant
            The reason is, that with auto instrumentation, the sub app is instrumented as well
            and therefore we would see the spans for the sub app as well

        This test is to ensure that a span in case of a sub app targeted contains the correct server url
        """

        self._client.get("/sub/home")
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 6)

        for span in spans:
            # As we are only looking to the "outer" app, we would see only the "GET /sub" spans
            #   -> the outer app is not aware of the sub_apps internal routes
            sub_in = "GET /sub" in span.name
            # The sub app spans are named GET /home as from the sub app perspective the request targets /home
            #   -> the sub app is technically not aware of the /sub prefix
            home_in = "GET /home" in span.name

            # We expect the spans to be either from the outer app or the sub app
            self.assertTrue(
                sub_in or home_in,
                f"Span {span.name} does not have /sub or /home in its name",
            )

        # We now want to specifically test all spans including the
        # - HTTP_TARGET
        # - HTTP_URL
        # attributes to be populated with the expected values
        spans_with_http_attributes = [
            span
            for span in spans
            if (
                SpanAttributes.HTTP_URL in span.attributes
                or SpanAttributes.HTTP_TARGET in span.attributes
            )
        ]

        # We now expect spans with attributes from both the app and its sub app
        self.assertEqual(2, len(spans_with_http_attributes))

        for span in spans_with_http_attributes:
            self.assertEqual(
                "/sub/home", span.attributes[SpanAttributes.HTTP_TARGET]
            )
        self.assertEqual(
            "https://testserver:443/sub/home",
            span.attributes[SpanAttributes.HTTP_URL],
        )


class TestAutoInstrumentationLogic(unittest.TestCase):
    def test_instrumentation(self):
        """Verify that instrumentation methods are instrumenting and
        removing as expected.
        """
        instrumentor = otel_fastapi.FastAPIInstrumentor()
        original = fastapi.FastAPI
        instrumentor.instrument()
        try:
            instrumented = fastapi.FastAPI
            self.assertIsNot(original, instrumented)
        finally:
            instrumentor.uninstrument()

        should_be_original = fastapi.FastAPI
        self.assertIs(original, should_be_original)
