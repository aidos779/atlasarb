"""Localization (PRD FR-LOC-01) — English, Russian, Kazakh.

A key-based catalog; `t(key, lang, **kwargs)` formats the localized string. Missing keys
fall back to English then to the key itself, so a rendering never crashes on a gap.
"""
from __future__ import annotations

from src.bot.i18n.catalog import CATALOG

_DEFAULT = "en"


def t(key: str, lang: str = _DEFAULT, /, **kwargs) -> str:
    lang = lang if lang in CATALOG else _DEFAULT
    value = CATALOG[lang].get(key) or CATALOG[_DEFAULT].get(key) or key
    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value
    return value


def available_languages() -> list[str]:
    return list(CATALOG.keys())
