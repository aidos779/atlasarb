"""Localization (PRD FR-LOC-01) — English and Russian.

A key-based catalog; ``t(key, lang, **kwargs)`` formats the localized string.

Deliberately NO cross-language fallback. A missing Russian key used to silently render
the English string, which is exactly the mixed-language UI this layer exists to prevent —
and it hid the gap from everyone except the user staring at it. Instead:

  * ``validate_catalog()`` raises on any gap and runs at startup, so an incomplete
    catalog cannot deploy;
  * a lookup that still misses at runtime logs an ERROR and renders a visible
    ``⟦key⟧`` marker, so the gap is loud but never crashes a live conversation.

Placeholders are validated too: a key whose ``{braces}`` differ across languages will
format correctly in one and silently drop data in the other, so that is a hard error.
"""
from __future__ import annotations

import re
from typing import Final

from src.config import get_logger
from src.i18n.catalog import CATALOG

log = get_logger("i18n")

DEFAULT_LANGUAGE: Final = "en"

#: Every language the bot ships. Each must be complete — see validate_catalog().
LANGUAGES: Final[tuple[str, ...]] = ("en", "ru")

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class MissingTranslationsError(RuntimeError):
    """Raised at startup when the catalog is not complete for every language."""


def _marker(key: str) -> str:
    return f"⟦{key}⟧"


def t(key: str, lang: str = DEFAULT_LANGUAGE, /, **kwargs) -> str:
    """Localized string for ``key``. Never falls back to another language."""
    catalog = CATALOG.get(lang)
    if catalog is None:
        log.error("i18n_unknown_language", language=lang, key=key)
        catalog = CATALOG[DEFAULT_LANGUAGE]
    value = catalog.get(key)
    if value is None:
        # Loud, not silent: a gap that reaches production is a bug, and rendering the
        # key makes it obvious in a screenshot instead of looking like intended copy.
        log.error("i18n_missing_key", key=key, language=lang)
        return _marker(key)
    if not kwargs:
        return value
    try:
        return value.format(**kwargs)
    except (KeyError, IndexError) as exc:
        log.error("i18n_format_failed", key=key, language=lang, error=str(exc))
        return value


def available_languages() -> list[str]:
    return list(LANGUAGES)


def translations_of(key: str) -> set[str]:
    """Every language's rendering of one key.

    The persistent reply keyboard sends its button label as plain text, so the handler
    has to match on the label itself. Deriving that set from the catalog keeps the match
    correct in every language and — unlike the literal sets this replaces — cannot drift
    when the copy is edited.
    """
    return {CATALOG[lang][key] for lang in LANGUAGES if key in CATALOG.get(lang, {})}


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def all_keys() -> set[str]:
    """Union of every key defined in any language — the completeness target."""
    keys: set[str] = set()
    for catalog in CATALOG.values():
        keys |= set(catalog)
    return keys


def missing_translations() -> dict[str, set[str]]:
    """Keys absent from each language. Empty dict means the catalog is complete."""
    target = all_keys()
    gaps = {lang: target - set(CATALOG.get(lang, {})) for lang in LANGUAGES}
    return {lang: keys for lang, keys in gaps.items() if keys}


def placeholder_mismatches() -> dict[str, tuple[set[str], set[str]]]:
    """Keys whose placeholders differ from the reference language.

    ``{lang:key: (expected, actual)}``. A mismatch means one language silently drops a
    value at format time — the kind of bug that only shows up for users of that language.
    """
    reference = CATALOG[DEFAULT_LANGUAGE]
    out: dict[str, tuple[set[str], set[str]]] = {}
    for lang in LANGUAGES:
        if lang == DEFAULT_LANGUAGE:
            continue
        for key, value in CATALOG.get(lang, {}).items():
            expected = placeholders(reference.get(key, ""))
            actual = placeholders(value)
            if expected != actual:
                out[f"{lang}:{key}"] = (expected, actual)
    return out


def validate_catalog() -> None:
    """Fail loudly if the catalog is incomplete or inconsistent.

    Called at startup (app.Application) and asserted by the test suite, so a missing
    translation is caught on deploy rather than by a user reading a half-English screen.
    """
    problems: list[str] = []
    for lang in LANGUAGES:
        if lang not in CATALOG:
            problems.append(f"language '{lang}' is missing from the catalog entirely")
    for lang, keys in missing_translations().items():
        listed = ", ".join(sorted(keys)[:20])
        more = f" (+{len(keys) - 20} more)" if len(keys) > 20 else ""
        problems.append(f"[{lang}] missing {len(keys)} key(s): {listed}{more}")
    for ref, (expected, actual) in placeholder_mismatches().items():
        problems.append(
            f"[{ref}] placeholder mismatch: expected {sorted(expected)}, got {sorted(actual)}")
    extra = set(CATALOG) - set(LANGUAGES)
    if extra:
        problems.append(f"catalog defines unsupported language(s): {sorted(extra)}")
    if problems:
        raise MissingTranslationsError(
            "Translation catalog is incomplete:\n  - " + "\n  - ".join(problems))
