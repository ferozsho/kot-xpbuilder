"""Validation for structured (JSON) provider output.

Every adapter can *ask* for JSON — Gemini sets ``response_mime_type``, the
OpenAI-shaped adapters set ``response_format``, Anthropic gets an instruction —
but a model can still answer with prose, a truncated document, a JSON array, or
a fenced code block. Phase 3 (P3-F10) therefore treats the response as untrusted
until it parses into a JSON **object** here.

The failure is reported as an ordinary :class:`ProviderError` with the stable
code ``structured_output_invalid`` and ``retryable=False``: the plan forbids a
blind fallback on validation failures, because the mismatch may be caused by the
prompt rather than the provider. Raw model text is never attached to the error
or logged — only the reason and the content length.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

#: A structured response larger than this is treated as malformed, not parsed.
MAX_STRUCTURED_BYTES = 262_144

#: Opening fence pairs models wrap JSON in despite an explicit instruction.
_FENCE_PREFIXES = ("```json", "```JSON", "```")


class StructuredOutputError(ValueError):
    """A provider response that is not a usable JSON object.

    ``code`` is stable so routing and audit can branch on it without parsing
    English, and ``str(error)`` never contains model output.
    """

    code = "structured_output_invalid"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_json_object(
    content: str,
    *,
    required_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Parse provider output into a JSON object or raise ``StructuredOutputError``.

    Accepts a bare object and the common ````` ```json … ``` ```` fenced form.
    Rejects arrays, scalars, invalid JSON, empty content, and objects missing any
    of ``required_keys`` — an absent key is a schema mismatch the caller must not
    discover halfway through building a query.
    """
    text = extract_json_text(content)
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"structured output is not valid JSON ({exc.msg})"
        ) from exc
    if not isinstance(decoded, dict):
        raise StructuredOutputError(
            f"structured output must be a JSON object, not {type(decoded).__name__}"
        )
    missing = [key for key in required_keys if key not in decoded]
    if missing:
        raise StructuredOutputError(
            "structured output is missing required key(s): " + ", ".join(missing)
        )
    return decoded


def extract_json_text(content: str) -> str:
    """Return the JSON body, unwrapping a Markdown code fence when present."""
    text = (content or "").strip()
    if not text:
        raise StructuredOutputError("structured output is empty")
    if len(text.encode("utf-8")) > MAX_STRUCTURED_BYTES:
        raise StructuredOutputError("structured output is too large")
    for prefix in _FENCE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            if text.endswith("```"):
                text = text[:-3].strip()
            break
    if not text:
        raise StructuredOutputError("structured output is empty")
    return text
