"""Provider adapters and routing for KOT5 XP Prompt."""

from xp_prompt.providers.anthropic import AnthropicProvider
from xp_prompt.providers.deepseek import DeepSeekProvider
from xp_prompt.providers.gemini import GeminiProvider
from xp_prompt.providers.openai_compatible import (
    AzureOpenAIProvider,
    OpenAICompatibleProvider,
)
from xp_prompt.providers.router import ProviderRouter

__all__ = [
    "AnthropicProvider",
    "AzureOpenAIProvider",
    "DeepSeekProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "ProviderRouter",
]

