"""Compatibility import for existing openai_api backends (Responses by default)."""

from .openai_compatible import OpenAICompatibleAdapter


class OpenAIAPIAdapter(OpenAICompatibleAdapter):
    pass
