"""Minimal JSON HTTP transport with normalized, redacted failures."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

from xp_prompt.providers.types import ProviderError


def validate_provider_endpoint(value: str, provider: str) -> str:
    """Validate a provider base URL without logging credentials or query data."""
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
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
            raise ProviderError(
                provider=provider,
                code=f"http_{exc.code}",
                message="provider request failed",
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

