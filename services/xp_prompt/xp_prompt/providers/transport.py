"""Minimal JSON HTTP transport with normalized, redacted failures."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

from xp_prompt.providers.types import ProviderError

#: Host suffixes that can only resolve inside a private deployment.
_LOCAL_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home.arpa")
_LOCAL_HOST_NAMES = frozenset(
    {"localhost", "host.docker.internal", "gateway.docker.internal"}
)

#: Longest provider explanation carried into an error message.
#:
#: The provider record stores ``last_test_error`` as VARCHAR(255), so the detail
#: is capped here to keep "http_402: ..." storable without a database error.
MAX_ERROR_DETAIL = 200

#: Characters that end a provider sentence worth keeping whole.
_SENTENCE_ENDS = (".", "!", "?", "\u3002")


def _truncate_detail(detail: str) -> str:
    """Shorten an over-long provider message without cutting mid-token.

    Google answers a depleted key with "RESOURCE_EXHAUSTED: <what to do>.
    Please go to AI Studio at <url> to manage your project and billing.
    Learn more at <url>". Cutting at the column limit produced a stored
    ".../gemini-api/docs/b", an unusable link. Ending on the last complete
    sentence keeps a readable instruction and a working URL.
    """
    if len(detail) <= MAX_ERROR_DETAIL:
        return detail
    window = detail[:MAX_ERROR_DETAIL]
    # A terminator only ends a sentence when whitespace follows it, which keeps
    # the dots inside host names ("ai.google.dev") from looking like sentence
    # ends and truncating a URL halfway through.
    sentences = [
        index
        for index, char in enumerate(window)
        if char in _SENTENCE_ENDS
        and (
            index + 1 == len(window)
            or (index + 1 < len(detail) and detail[index + 1] == " ")
        )
    ]
    # A sentence end very early in the window would drop the actual cause, so
    # only honour it when it still carries most of the explanation.
    if sentences and sentences[-1] >= MAX_ERROR_DETAIL // 4:
        return window[: sentences[-1] + 1]
    space = window.rfind(" ")
    if space > 0:
        return f"{window[:space].rstrip()}..."
    return window


def _provider_error_detail(exc: urllib.error.HTTPError) -> str:
    """The provider's own explanation for an HTTP failure, or ''.

    An administrator saw only "http_402" for a Google key whose prepayment
    credits were depleted, which reads like a connector bug instead of a billing
    condition. Reading the structured ``error`` object restores the sentence the
    provider actually sent.

    Only ``error.status`` and ``error.message`` are read: the raw body is never
    returned or logged because it can echo request content or credentials.
    """
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - a missing body must not mask the failure
        return ""
    if not raw:
        return ""
    try:
        decoded = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(decoded, dict):
        return ""
    error = decoded.get("error")
    if not isinstance(error, dict):
        return ""
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return ""
    detail = " ".join(message.split())
    status = error.get("status")
    if isinstance(status, str) and status.strip():
        detail = f"{status.strip()}: {detail}"
    return _truncate_detail(detail)


def _is_local_host(host: str) -> bool:
    """Is this host loopback, private, or a single-label container name?

    A local model server (Ollama, LM Studio, vLLM) is a supported deployment:
    it cannot present a certificate, so plain HTTP is allowed for hosts that
    never leave the private network. Public hosts stay HTTPS-only because their
    credentials would otherwise cross the internet in clear text.
    """
    name = host.lower().rstrip(".")
    if name in _LOCAL_HOST_NAMES or name.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        # A single-label name such as "ollama" resolves only via Docker DNS.
        return "." not in name
    return address.is_loopback or address.is_private or address.is_link_local


def validate_provider_endpoint(
    value: str,
    provider: str,
    *,
    allow_local_http: bool = False,
) -> str:
    """Validate a provider base URL without logging credentials or query data."""
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    if not parsed.hostname:
        raise ValueError(f"{provider} endpoint must be an absolute HTTPS URL")
    if parsed.scheme != "https" and not (
        allow_local_http and parsed.scheme == "http" and _is_local_host(parsed.hostname)
    ):
        raise ValueError(f"{provider} endpoint must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            f"{provider} endpoint must not contain credentials, query, or fragment"
        )
    return endpoint


class JsonTransport(Protocol):
    def post_json(
        self,
        *,
        provider: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: int,
    ) -> tuple[dict[str, Any], Mapping[str, str]]: ...


class UrlLibJsonTransport:
    """Standard-library transport to avoid provider-SDK coupling."""

    def post_json(
        self,
        *,
        provider: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: int,
    ) -> tuple[dict[str, Any], Mapping[str, str]]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", **dict(headers)},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("provider response must be a JSON object")
                return decoded, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After")
            detail = _provider_error_detail(exc)
            raise ProviderError(
                provider=provider,
                code=f"http_{exc.code}",
                message=detail or "provider request failed",
                retryable=exc.code in {408, 409, 429} or 500 <= exc.code <= 599,
                status_code=exc.code,
                retry_after_seconds=(
                    int(retry_after) if retry_after and retry_after.isdigit() else None
                ),
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(
                provider=provider,
                code="transport_error",
                message="provider transport failed",
                retryable=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                provider=provider,
                code="invalid_response",
                message="provider returned an invalid response",
                retryable=False,
            ) from exc

