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

