"""Transport failure fixtures (Phase 3, P3-F09).

Each scenario pins the *normalized* error the router depends on, so a provider
outage or a rate limit can never be mistaken for a validation failure:

===========================  ==============  =========  ======================
Scenario                     code            retryable  meaning for routing
===========================  ==============  =========  ======================
HTTP 429 + Retry-After       http_429        yes        wait, then retry once
HTTP 503                     http_503        yes        retry, then fall back
HTTP 401 (bad key)           http_401        no         stop, surface to admin
HTTP 402 (no credits)        http_402        no         stop, fund the account
Socket timeout               transport_error yes        retry, then fall back
Non-JSON body                invalid_response no         stop, provider is broken
JSON array body              invalid_response no         stop, contract violation
===========================  ==============  =========  ======================

``ProviderErrorDetailTest`` additionally pins that an HTTP failure carries the
provider's own explanation (e.g. ``RESOURCE_EXHAUSTED: prepayment credits are
depleted``) instead of a bare status code, and that unreadable bodies fall back
to the generic sentence.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from xp_prompt.providers.transport import MAX_ERROR_DETAIL, UrlLibJsonTransport
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


class ProviderErrorDetailTest(unittest.TestCase):
    """The provider's own sentence must survive into the normalized failure.

    A Google key whose prepayment credits were depleted answered HTTP 402 with a
    structured body. Storing only ``http_402`` made a billing condition read like
    a connector defect, so the transport now carries the message through.
    """

    def http_error(self, code: int, body: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(URL, code, "error", {}, io.BytesIO(body))

    def fail_with(self, code: int, body: bytes) -> ProviderError:
        with (
            mock.patch(
                "xp_prompt.providers.transport.urllib.request.urlopen",
                side_effect=self.http_error(code, body),
            ),
            self.assertRaises(ProviderError) as raised,
        ):
            post()
        return raised.exception

    def test_structured_error_body_surfaces_the_provider_message(self) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": 402,
                    "message": "Your prepayment credits are depleted.",
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
        ).encode()

        failure = self.fail_with(402, body)

        self.assertEqual(failure.code, "http_402")
        self.assertEqual(
            str(failure),
            "RESOURCE_EXHAUSTED: Your prepayment credits are depleted.",
        )
        self.assertFalse(failure.retryable)

    def test_non_json_error_body_falls_back_to_the_generic_message(self) -> None:
        failure = self.fail_with(502, b"<html>upstream gateway</html>")

        self.assertEqual(failure.code, "http_502")
        self.assertEqual(str(failure), "provider request failed")

    def test_error_body_without_an_error_object_falls_back(self) -> None:
        failure = self.fail_with(400, json.dumps({"detail": "bad"}).encode())

        self.assertEqual(str(failure), "provider request failed")

    def test_blank_provider_message_falls_back(self) -> None:
        failure = self.fail_with(
            403, json.dumps({"error": {"message": "   "}}).encode()
        )

        self.assertEqual(str(failure), "provider request failed")

    def test_empty_error_body_falls_back(self) -> None:
        failure = self.fail_with(402, b"")

        self.assertEqual(failure.code, "http_402")
        self.assertEqual(str(failure), "provider request failed")

    def test_error_detail_is_capped_to_the_storage_column(self) -> None:
        body = json.dumps({"error": {"message": "x" * 1000}}).encode()

        failure = self.fail_with(500, body)

        self.assertEqual(str(failure), "x" * MAX_ERROR_DETAIL)
        self.assertLessEqual(len(str(failure)), 255)

    def test_long_detail_ends_on_a_complete_sentence(self) -> None:
        # The live Google 402 body, verbatim. A column-limit cut produced
        # ".../gemini-api/docs/b", a link an administrator cannot follow.
        body = json.dumps(
            {
                "error": {
                    "message": (
                        "Your prepayment credits are depleted. Please go to AI "
                        "Studio at https://ai.studio/projects to manage your "
                        "project and billing. Learn more at "
                        "https://ai.google.dev/gemini-api/docs/billing"
                    ),
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
        ).encode()

        detail = str(self.fail_with(402, body))

        self.assertIn("prepayment credits are depleted", detail)
        self.assertTrue(
            detail.endswith("to manage your project and billing."),
            msg=f"detail was cut mid-sentence: {detail!r}",
        )
        self.assertNotIn("docs/b", detail)
        self.assertLessEqual(len(detail), MAX_ERROR_DETAIL)


if __name__ == "__main__":
    unittest.main()
