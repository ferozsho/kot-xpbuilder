"""Tests for admin-managed AI provider configuration (settings page backing)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from xp_prompt.config import Settings
from xp_prompt.providers import registry
from xp_prompt.providers.anthropic import AnthropicProvider
from xp_prompt.providers.deepseek import DeepSeekProvider
from xp_prompt.providers.gemini import GeminiProvider
from xp_prompt.providers.openai_compatible import (
    AzureOpenAIProvider,
    OpenAICompatibleProvider,
)
from xp_prompt.security import (
    SecretBox,
    constant_time_equals,
    internal_admin_token,
    key_fingerprint,
    key_last_four,
)

ROOT_SECRET = "unit-test-root-secret-value"
OTHER_SECRET = "a-different-root-secret-value"


def base_env() -> dict[str, str]:
    return {
        "XP_PROMPT_POSTGRES_HOST": "xp-prompt-db",
        "XP_PROMPT_POSTGRES_PORT": "5432",
        "XP_PROMPT_POSTGRES_USER": "xprompt",
        "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
        "XP_PROMPT_POSTGRES_DB": "xprompt",
        "XP_PROMPT_REDIS_HOST": "superset-redis",
        "XP_PROMPT_REDIS_PORT": "6379",
        "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
        "XP_PROMPT_REDIS_DB": "2",
        "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
        "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
    }


def provider_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "OpenAI (prod)",
        "provider_type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "config": {"temperature": 0.7, "max_tokens": 1024, "timeout_seconds": 60},
        "enabled": True,
        "is_default": True,
        "priority": 0,
    }
    payload.update(overrides)
    return payload


def provider_record(**overrides: object) -> registry.ProviderRecord:
    values: dict[str, object] = {
        "id": "2f1c3d64-5c4a-4d80-9b0f-1c3a4a2b5e11",
        "tenant_key": "default",
        "name": "OpenAI (prod)",
        "provider_type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "config": {"temperature": 0.7},
        "enabled": True,
        "is_default": True,
        "priority": 0,
        "api_key_fingerprint": "a" * 12,
        "api_key_last_four": "9f2c",
        "last_tested_at": datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        "last_test_status": "succeeded",
        "last_test_error": None,
    }
    values.update(overrides)
    return registry.ProviderRecord(**values)  # type: ignore[arg-type]


class SecretBoxTest(unittest.TestCase):
    def test_round_trip_preserves_plaintext(self) -> None:
        box = SecretBox.from_root_secret(ROOT_SECRET)
        sealed = box.encrypt("sk-live-abcdef123456")

        self.assertNotIn("sk-live", sealed)
        self.assertEqual(box.decrypt(sealed), "sk-live-abcdef123456")

    def test_ciphertext_is_not_reversible_with_another_secret(self) -> None:
        sealed = SecretBox.from_root_secret(ROOT_SECRET).encrypt("sk-live-abcdef")

        with self.assertRaisesRegex(RuntimeError, "cannot be decrypted"):
            SecretBox.from_root_secret(OTHER_SECRET).decrypt(sealed)

    def test_empty_key_is_rejected(self) -> None:
        box = SecretBox.from_root_secret(ROOT_SECRET)

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            box.encrypt("   ")

    def test_short_root_secret_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "at least 16 characters"):
            SecretBox.from_root_secret("short")

    def test_fingerprint_is_stable_and_non_reversible(self) -> None:
        fingerprint = key_fingerprint("sk-live-abcdef123456")

        self.assertEqual(len(fingerprint), 12)
        self.assertEqual(fingerprint, key_fingerprint("sk-live-abcdef123456"))
        self.assertNotIn("sk-live", fingerprint)
        self.assertEqual(key_last_four("sk-live-abcdef123456"), "3456")
        self.assertEqual(key_last_four("short"), "")


class InternalTokenTest(unittest.TestCase):
    def test_token_is_deterministic_and_secret_dependent(self) -> None:
        first = internal_admin_token(ROOT_SECRET)

        self.assertEqual(first, internal_admin_token(ROOT_SECRET))
        self.assertNotEqual(first, internal_admin_token(OTHER_SECRET))
        self.assertEqual(len(first), 64)

    def test_constant_time_compare(self) -> None:
        token = internal_admin_token(ROOT_SECRET)

        self.assertTrue(constant_time_equals(token, token))
        self.assertFalse(constant_time_equals(token, "x" * 64))


class ProviderInputTest(unittest.TestCase):
    def test_parse_applies_defaults(self) -> None:
        parsed = registry.ProviderInput.parse(
            {
                "name": "Gemini",
                "provider_type": "gemini",
                "default_model": "gemini-2.0-flash",
            }
        )

        self.assertEqual(parsed.provider_type, "gemini")
        self.assertEqual(
            parsed.base_url, "https://generativelanguage.googleapis.com/v1beta"
        )
        self.assertEqual(parsed.temperature, 0.7)
        self.assertEqual(parsed.max_tokens, 1024)
        self.assertEqual(parsed.timeout_seconds, 45)
        self.assertTrue(parsed.enabled)

    def test_azure_requires_an_explicit_base_url(self) -> None:
        with self.assertRaisesRegex(
            registry.ProviderConfigError, "Base URL is required"
        ):
            registry.ProviderInput.parse(
                {
                    "name": "Azure",
                    "provider_type": "azure_openai",
                    "default_model": "gpt-4o-mini",
                }
            )

    def test_api_version_is_stored_only_for_azure(self) -> None:
        azure = registry.ProviderInput.parse(
            {
                "name": "Azure",
                "provider_type": "azure_openai",
                "base_url": "https://example.openai.azure.com",
                "default_model": "gpt-4o-mini",
                "config": {"api_version": "2024-10-21"},
            }
        )
        openai = registry.ProviderInput.parse({**provider_payload()})

        self.assertEqual(azure.config["api_version"], "2024-10-21")
        self.assertNotIn("api_version", openai.config)

    def test_plain_http_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(registry.ProviderConfigError, "absolute HTTPS"):
            registry.ProviderInput.parse(
                provider_payload(base_url="http://api.openai.com/v1")
            )

    def test_private_http_endpoint_is_accepted_for_local_models(self) -> None:
        """The settings page documents http://localhost:11434/v1 for Ollama."""
        cases = (
            "http://localhost:11434/v1",
            "http://127.0.0.1:11434/v1",
            "http://ollama:11434/v1",
            "http://gpu-box.local:8000/v1",
            "http://192.168.1.50:8080/v1",
        )
        for base_url in cases:
            with self.subTest(base_url=base_url):
                parsed = registry.ProviderInput.parse(
                    provider_payload(base_url=base_url)
                )
                self.assertEqual(parsed.base_url, base_url)

    def test_public_http_endpoint_with_a_dotted_name_is_still_rejected(self) -> None:
        with self.assertRaisesRegex(registry.ProviderConfigError, "absolute HTTPS"):
            registry.ProviderInput.parse(
                provider_payload(base_url="http://api.example.com/v1")
            )

    def test_unknown_provider_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            registry.ProviderConfigError, "Provider type must be one of"
        ):
            registry.ProviderInput.parse(provider_payload(provider_type="mistral"))

    def test_limits_are_enforced(self) -> None:
        cases = (
            ({"temperature": 9}, "Temperature"),
            ({"max_tokens": 0}, "Max tokens"),
            ({"timeout_seconds": 9999}, "Timeout"),
        )
        for config, label in cases:
            with self.subTest(config=config):
                with self.assertRaisesRegex(registry.ProviderConfigError, label):
                    registry.ProviderInput.parse(
                        provider_payload(config={**config})
                    )

    def test_display_name_is_required(self) -> None:
        with self.assertRaisesRegex(
            registry.ProviderConfigError, "Display name is required"
        ):
            registry.ProviderInput.parse(provider_payload(name="   "))

    def test_priority_bounds(self) -> None:
        with self.assertRaisesRegex(registry.ProviderConfigError, "Priority"):
            registry.ProviderInput.parse(provider_payload(priority=5000))


class ProviderRecordTest(unittest.TestCase):
    def test_public_dict_never_exposes_key_material(self) -> None:
        public = provider_record().to_public_dict()
        rendered = repr(public)

        self.assertNotIn("api_key_ciphertext", public)
        self.assertEqual(public["api_key"]["last_four"], "9f2c")
        self.assertTrue(public["api_key"]["configured"])
        self.assertNotIn("sk-", rendered)
        self.assertEqual(public["last_test"]["status"], "succeeded")

    def test_router_dict_is_minimal(self) -> None:
        router_dict = provider_record().to_router_dict()

        self.assertNotIn("config", router_dict)
        self.assertNotIn("api_key", router_dict)
        self.assertEqual(router_dict["provider_type"], "openai_compatible")


class RegistryHelperTest(unittest.TestCase):
    def test_provider_types_cover_the_settings_page_options(self) -> None:
        keys = {entry["key"] for entry in registry.provider_types_payload()}

        self.assertSetEqual(
            keys,
            {
                "openai_compatible",
                "anthropic",
                "gemini",
                "azure_openai",
                "deepseek",
            },
        )

    def test_build_provider_selects_the_matching_adapter(self) -> None:
        cases = (
            ("openai_compatible", OpenAICompatibleProvider),
            ("anthropic", AnthropicProvider),
            ("gemini", GeminiProvider),
            ("deepseek", DeepSeekProvider),
            ("azure_openai", AzureOpenAIProvider),
        )
        for provider_type, expected in cases:
            with self.subTest(provider_type=provider_type):
                record = provider_record(
                    provider_type=provider_type,
                    base_url="https://api.example.com/v1",
                )
                provider = registry.build_provider(record, "sk-test-1234567890")
                self.assertIsInstance(provider, expected)

    def test_provider_ids_are_uuid_checked(self) -> None:
        self.assertTrue(
            registry.is_provider_id("2f1c3d64-5c4a-4d80-9b0f-1c3a4a2b5e11")
        )
        self.assertFalse(registry.is_provider_id("not-a-uuid"))

    def test_a_local_model_endpoint_survives_adapter_construction(self) -> None:
        """Building an adapter must not re-tighten the local-HTTP allowance."""
        record = provider_record(base_url="http://localhost:11434/v1")

        provider = registry.build_provider(record, "ollama-key-123456")

        self.assertEqual(provider.endpoint, "http://localhost:11434/v1")


class ServiceSecretSettingsTest(unittest.TestCase):
    def test_service_secret_comes_from_env_or_superset_secret(self) -> None:
        env = base_env()
        self.assertEqual(Settings.from_env(env).service_secret, ROOT_SECRET)

        env.pop("XP_PROMPT_SERVICE_SECRET")
        env["SUPERSET_SECRET_KEY"] = "superset-root-secret-value"
        self.assertEqual(
            Settings.from_env(env).service_secret, "superset-root-secret-value"
        )

    def test_service_secret_is_redacted(self) -> None:
        settings = Settings.from_env(base_env())
        summary = settings.safe_summary()

        self.assertTrue(summary["provider_admin_ready"])
        self.assertNotIn(ROOT_SECRET, repr(settings))
        self.assertNotIn(ROOT_SECRET, repr(summary))
        self.assertNotIn("service_secret", summary)


class ResolveRouterTest(unittest.TestCase):
    """The settings page is the source of truth for live AI routing."""

    def setUp(self) -> None:
        self.settings = Settings.from_env(base_env())

    def test_no_provider_anywhere_fails_with_actionable_message(self) -> None:
        with mock.patch.object(registry, "list_providers", return_value=[]):
            with self.assertRaisesRegex(
                registry.ProviderConfigError, r"Settings -> AI Provider"
            ):
                registry.resolve_router(self.settings)

    def test_saved_provider_wins_over_the_env_key(self) -> None:
        env_settings = Settings.from_env(
            {
                **base_env(),
                "GOOGLE_GEMINI_API_KEY": "env-gemini-key",
                "XP_PROMPT_GEMINI_MODEL": "gemini-2.0-flash",
            }
        )
        record = provider_record(
            name="Gemini (team)",
            provider_type="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            default_model="gemini-2.0-flash",
        )

        with mock.patch.object(
            registry, "list_providers", return_value=[record]
        ), mock.patch.object(
            registry, "decrypt_api_key", return_value="sealed-gemini-key"
        ):
            router = registry.resolve_router(env_settings)

        self.assertEqual(router.primary.name, "Gemini (team)")
        self.assertEqual(router.primary.model, "gemini-2.0-flash")
        self.assertIsNone(router.fallback)

    def test_second_saved_provider_is_the_fallback(self) -> None:
        primary = provider_record(name="OpenAI (prod)")
        fallback = provider_record(
            id="9c5c1b2e-3d4f-4a61-8b7c-0d1e2f3a4b5c",
            name="Anthropic (prod)",
            provider_type="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-3-5-sonnet-latest",
            is_default=False,
            priority=10,
        )

        with mock.patch.object(
            registry, "list_providers", return_value=[primary, fallback]
        ), mock.patch.object(
            registry, "decrypt_api_key", return_value="sealed-key-value"
        ):
            router = registry.resolve_router(self.settings)

        self.assertEqual(router.primary.name, "OpenAI (prod)")
        self.assertEqual(router.fallback.name, "Anthropic (prod)")

    def test_undecryptable_key_falls_back_to_the_env_provider(self) -> None:
        env_settings = Settings.from_env(
            {
                **base_env(),
                "GOOGLE_GEMINI_API_KEY": "env-gemini-key",
                "XP_PROMPT_GEMINI_MODEL": "gemini-2.0-flash",
            }
        )
        record = provider_record(
            name="Gemini (team)",
            provider_type="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            default_model="gemini-2.0-flash",
        )

        with mock.patch.object(
            registry, "list_providers", return_value=[record]
        ), mock.patch.object(
            registry,
            "decrypt_api_key",
            side_effect=RuntimeError("stored provider API key cannot be decrypted"),
        ):
            router = registry.resolve_router(env_settings)

        self.assertEqual(router.primary.name, "gemini (.env)")
        self.assertIsNone(router.fallback)

    def test_disabled_provider_is_not_routed(self) -> None:
        record = provider_record(name="Disabled", enabled=False)

        with mock.patch.object(
            registry, "list_providers", return_value=[record]
        ), mock.patch.object(
            registry, "decrypt_api_key", return_value="sealed-key-value"
        ):
            with self.assertRaises(registry.ProviderConfigError):
                registry.resolve_router(self.settings)

    def test_unmigrated_database_does_not_break_routing(self) -> None:
        with mock.patch.object(
            registry, "list_providers", side_effect=RuntimeError("relation missing")
        ):
            with self.assertRaises(registry.ProviderConfigError):
                registry.resolve_router(self.settings)


if __name__ == "__main__":
    unittest.main()
