"""Loader for the versioned prompt templates in prompts/.

Each template is one markdown file split into a system part and a user part:

    <!-- system -->
    ...instructions that never change...
    <!-- user -->
    ...request body with $placeholders...

Placeholders use string.Template ($name) rather than str.format, because the
templates quote C++ and Rust code full of braces.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_SYSTEM_MARKER = "<!-- system -->"
_USER_MARKER = "<!-- user -->"


class PromptError(Exception):
    """Raised when a prompt template is missing or malformed."""


@lru_cache(maxsize=None)
def _load(name: str) -> tuple[str, str]:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"Prompt template not found: {path}")
    text = path.read_text(encoding="utf-8")

    if _SYSTEM_MARKER not in text or _USER_MARKER not in text:
        raise PromptError(f"{path} must contain both {_SYSTEM_MARKER} and {_USER_MARKER}")
    _, remainder = text.split(_SYSTEM_MARKER, maxsplit=1)
    system, user = remainder.split(_USER_MARKER, maxsplit=1)
    return system.strip(), user.strip()


def render(name: str, **values: str) -> tuple[str, str]:
    """Return the (system, user) messages for a template, substituting $placeholders."""
    system, user = _load(name)
    try:
        return system, Template(user).substitute(**values)
    except KeyError as exc:
        raise PromptError(f"Missing placeholder {exc} for prompt {name!r}") from exc


def available() -> list[str]:
    """Names of the prompt templates on disk."""
    return sorted(path.stem for path in PROMPTS_DIR.glob("*.md"))
