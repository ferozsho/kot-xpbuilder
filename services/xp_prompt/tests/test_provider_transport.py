"""Transport failure fixtures (Phase 3, P3-F09).

Each scenario pins the *normalized* error the router depends on, so a provider
outage or a rate limit can never be mistaken for a validation failure:

===========================  ==============  =========  ======================
Scenario                     code            retryable  meaning for routing
===========================  ==============  =========  ======================
HTTP 429 + Retry-After       http_429        yes        wait, then retry once
HTTP 503                     http_503        yes        retry, then fall back
HTTP 401 (bad key)           http_401        no         stop, surface to admin
Socket timeout               transport_error yes        retry, then fall back
Non-JSON body                invalid_response no        stop, provider is broken
JSON array body              invalid_response no        stop, contract violation
===========================  ==============  =========  ======================
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from xp_prompt.providers.transport import UrlLibJsonTransport
from xp_prompt.providers.types import ProviderError

PROVIDER = "gemini"
URL = "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"


class FakeRawResponse:
    """Minimal stand-in for the ``urlopen`` context manager."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeRawResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def post(transport: UrlLibJsonTransport | None = None) -> None:
    (transport or UrlLibJsonTransport()).post_json(
        provider=PROVIDER,
        url=URL,
        headers={"X-Test": "1"},
        payload={"hello": "world"},
        timeout_seconds=5,
    )


class TransportErrorFixtureTest(unittest.TestCase):
    def test_rate_limit_keeps_the_retry_after_hint(self) -> None:
        error = urllib.error.HTTPError(
            URL, 429, "Too Many Requests", {"Retry-After": "30"}, None
        )

        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        failure = raised.exception
        self.assertEqual(failure.code, "http_429")
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.retry_after_seconds, 30)
        self.assertEqual(failure.status_code, 429)

    def test_outage_is_retryable(self) -> None:
        error = urllib.error.HTTPError(URL, 503, "Service Unavailable", {}, None)

        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        self.assertEqual(raised.exception.code, "http_503")
        self.assertTrue(raised.exception.retryable)

    def test_rejected_credentials_are_not_retryable(self) -> None:
        error = urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None)

        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        self.assertEqual(raised.exception.code, "http_401")
        self.assertFalse(raised.exception.retryable)

    def test_socket_timeout_is_a_retryable_transport_error(self) -> None:
        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        self.assertEqual(raised.exception.code, "transport_error")
        self.assertTrue(raised.exception.retryable)
        self.assertIsNone(raised.exception.status_code)

    def test_connection_refused_is_a_retryable_transport_error(self) -> None:
        refused = urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            side_effect=refused,
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        self.assertEqual(raised.exception.code, "transport_error")
        self.assertTrue(raised.exception.retryable)

    def test_non_json_body_is_not_retryable(self) -> None:
        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            return_value=FakeRawResponse(b"<html>gateway error</html>"),
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertFalse(raised.exception.retryable)

    def test_json_array_body_violates_the_object_contract(self) -> None:
        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            return_value=FakeRawResponse(json.dumps([{"candidates": []}]).encode()),
        ):
            with self.assertRaises(ProviderError) as raised:
                post()

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertFalse(raised.exception.retryable)

    def test_successful_body_is_returned_with_headers(self) -> None:
        body = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}

        with mock.patch(
            "xp_prompt.providers.transport.urllib.request.urlopen",
            return_value=FakeRawResponse(
                json.dumps(body).encode(), {"X-Request-Id": "req-9"}
            ),
        ):
            decoded, headers = UrlLibJsonTransport().post_json(
                provider=PROVIDER,
                url=URL,
                headers={"X-Test": "1"},
                payload={"hello": "world"},
                timeout_seconds=5,
            )

        self.assertEqual(decoded, body)
        self.assertEqual(headers["X-Request-Id"], "req-9")


if __name__ == "__main__":
    unittest.main()
