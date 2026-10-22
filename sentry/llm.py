"""Structured LLM calls for the review graph.

The provider is selected with SENTRY_LLM_PROVIDER (anthropic | openai) and the
model with SENTRY_LLM_MODEL. Both backends use their vendor's official SDK and
its native structured-output support, so a node always gets back a validated
pydantic model rather than free-form text it has to parse.
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5",
}

_MAX_TOKENS = 16000

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the configured LLM provider cannot produce a structured answer."""


def provider() -> str:
    name = os.environ.get("SENTRY_LLM_PROVIDER", "anthropic").strip().lower()
    if name not in DEFAULT_MODELS:
        allowed = ", ".join(sorted(DEFAULT_MODELS))
        raise LLMError(f"Unknown SENTRY_LLM_PROVIDER {name!r} (expected one of: {allowed})")
    return name


def model() -> str:
    configured = os.environ.get("SENTRY_LLM_MODEL", "").strip()
    return configured or DEFAULT_MODELS[provider()]


def _api_key_variable() -> str:
    return "ANTHROPIC_API_KEY" if provider() == "anthropic" else "OPENAI_API_KEY"


def is_configured() -> bool:
    """True when the selected provider has an API key available."""
    try:
        return bool(os.environ.get(_api_key_variable()))
    except LLMError:
        return False


def describe() -> str:
    """Short 'provider/model' label for the shell and for run reports."""
    try:
        return f"{provider()}/{model()}"
    except LLMError as exc:
        return f"misconfigured ({exc})"


def require_configured() -> None:
    if not is_configured():
        raise LLMError(
            f"No API key for the {provider()} provider (set {_api_key_variable()})"
        )


def _complete_anthropic(system: str, user: str, schema: type[T]) -> T:
    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=model(),
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except anthropic.APIError as exc:
        raise LLMError(f"Anthropic API error: {exc}") from exc

    # A policy decline is a legitimate outcome here: it is reported in the PR
    # feedback rather than rerouted, so the reviewer sees why a node produced
    # nothing.
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        raise LLMError(f"The model declined this request (category: {category})")
    if response.parsed_output is None:
        raise LLMError("The model returned no structured output")
    return response.parsed_output


def _complete_openai(system: str, user: str, schema: type[T]) -> T:
    import openai

    client = openai.OpenAI()
    try:
        response = client.responses.parse(
            model=model(),
            instructions=system,
            input=user,
            max_output_tokens=_MAX_TOKENS,
            text_format=schema,
        )
    except openai.OpenAIError as exc:
        raise LLMError(f"OpenAI API error: {exc}") from exc

    if response.output_parsed is None:
        raise LLMError("The model returned no structured output")
    return response.output_parsed


def complete_structured(system: str, user: str, schema: type[T]) -> T:
    """Ask the configured model for an answer validated against `schema`."""
    require_configured()
    if provider() == "anthropic":
        return _complete_anthropic(system, user, schema)
    return _complete_openai(system, user, schema)
