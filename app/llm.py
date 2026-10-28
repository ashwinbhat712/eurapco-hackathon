"""datapizza-ai client factory shared by the supervisor and the subagents."""

import os

from anthropic import AsyncAnthropic


def model_name():
    return os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")


def make_client(provider=None):
    """Return a datapizza AnthropicClient whose async transport is `provider` (or a new AsyncAnthropic).

    datapizza-ai-clients-anthropic declares anthropic<1.0 but only uses messages.create/parse,
    which the kit's pinned anthropic 1.x provides; install it with --no-deps (see README).
    """
    try:
        from datapizza.clients.anthropic import AnthropicClient
    except ImportError as exc:
        raise RuntimeError(
            "datapizza Anthropic client missing: pip install --no-deps datapizza-ai-clients-anthropic==0.0.8"
        ) from exc
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = AnthropicClient(api_key=api_key, model=model_name())
    client.a_client = provider or AsyncAnthropic(api_key=api_key, max_retries=0, timeout=45)
    return client
