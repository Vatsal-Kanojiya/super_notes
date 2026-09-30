"""Which provider class a name resolves to.

Import paths as strings, resolved lazily, as in the reference: a provider's
module is imported only when it is the configured one, so a broken or
half-configured provider cannot stop the others from loading.
"""

from django.utils.module_loading import import_string

from .errors import ChatError

PROVIDERS = {
    "fake": "assistant.chat.providers.fake.FakeProvider",
    "claude": "assistant.chat.providers.claude.ClaudeProvider",
    "gemini": "assistant.chat.providers.gemini.GeminiProvider",
    "openai": "assistant.chat.providers.openai.OpenAIProvider",
}


def get_provider(name: str):
    """Return a fresh instance of the provider registered under `name`."""
    path = PROVIDERS.get(name)
    if path is None:
        raise ChatError(f"Unknown chat provider: {name!r}")

    try:
        provider_class = import_string(path)
    except ImportError as exc:
        # A deployment problem, not a question the model failed to answer,
        # but it still has to surface as ChatError -- the only permanent
        # failure this package's boundary promises to raise.
        raise ChatError(f"Chat provider {name!r} is not available: {exc}") from exc

    return provider_class()
