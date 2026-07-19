"""Localization guarantees (FR-LOC-01).

The bot ships en/ru. These tests pin the four properties that make that real:

  1. completeness — every key exists in every language, with matching placeholders;
  2. no silent fallback — a missing key is reported, not quietly served in English;
  3. no mixed languages — the Russian catalog contains no stray English and vice versa,
     proper nouns excepted;
  4. no hardcoded copy — handlers render through the catalog, and every key they ask for
     actually exists.

Property 4 is the one that rots: a new handler with a literal string passes every other
test. The source scan below is what catches it in review instead of in production.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.domain.enums import ArbitrageType, Language, RiskScore, SignalStatus
from src.i18n import LANGUAGES, t, translations_of, validate_catalog
from src.i18n.catalog import CATALOG
from src.i18n.translator import (
    MissingTranslationsError,
    all_keys,
    missing_translations,
    placeholder_mismatches,
    placeholders,
)

SRC = Path(__file__).resolve().parents[2] / "src"

#: Proper nouns that legitimately stay in Latin script inside Russian copy — brand and
#: product names, tickers, venue names, plan names, and bot commands.
_ALLOWED_LATIN = {
    "crypto", "arbitrage", "scanner", "telegram", "payments", "binance", "okx",
    "bitget", "mexc", "uniswap", "pancakeswap", "jupiter", "sushiswap",
    "usdt", "btc", "eth", "sol", "bnb", "cex", "dex", "mrr", "api", "id", "p95",
    "free", "basic", "pro", "ms", "faq",
    # bot commands and placeholder names, which are identifiers rather than prose
    "start", "menu", "signals", "favorites", "profile", "subscription", "settings",
    "search", "history", "help", "cancel",
    # "p" is the residue of the metric name "p95" once digits are dropped
    "p",
    # Telegram API field names an admin types verbatim ("user_id", "@username") —
    # translating them would make the instruction wrong.
    "user", "username",
}

_BRACED = re.compile(r"\{\w+\}")


def _prose(text: str) -> str:
    """Copy with placeholders removed — ``{tier}`` is an identifier, not English prose."""
    return _BRACED.sub(" ", text)

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
# Matches t("some.key" ...) — the literal-key call form used throughout the bot.
_T_CALL = re.compile(r"\bt\(\s*[\"']([a-z][a-z0-9_.]*)[\"']")


# ── 1. completeness ──
def test_catalog_validates():
    validate_catalog()          # raises MissingTranslationsError on any gap


def test_no_missing_translations():
    assert missing_translations() == {}


def test_placeholders_match_across_languages():
    assert placeholder_mismatches() == {}


def test_every_language_has_every_key():
    target = all_keys()
    for lang in LANGUAGES:
        assert set(CATALOG[lang]) == target, f"{lang} diverges from the key set"


def test_validate_catalog_detects_an_injected_gap(monkeypatch):
    """The guard must actually fire — a completeness check that cannot fail is useless."""
    broken = {lang: dict(keys) for lang, keys in CATALOG.items()}
    del broken["ru"]["menu.title"]
    monkeypatch.setattr("src.i18n.translator.CATALOG", broken)
    with pytest.raises(MissingTranslationsError, match="menu.title"):
        validate_catalog()


def test_validate_catalog_detects_placeholder_drift(monkeypatch):
    broken = {lang: dict(keys) for lang, keys in CATALOG.items()}
    broken["ru"]["signals.page"] = "Стр. {current} из {итого}"   # renamed placeholder
    monkeypatch.setattr("src.i18n.translator.CATALOG", broken)
    with pytest.raises(MissingTranslationsError, match="placeholder mismatch"):
        validate_catalog()


# ── 2. no silent fallback ──
def test_missing_key_renders_a_marker_not_english(monkeypatch, caplog):
    broken = {lang: dict(keys) for lang, keys in CATALOG.items()}
    del broken["ru"]["menu.title"]
    monkeypatch.setattr("src.i18n.translator.CATALOG", broken)

    rendered = t("menu.title", "ru")

    # The decisive assertion: the English string must NOT appear. Falling back to it is
    # what produced half-English screens that nobody noticed.
    assert rendered != CATALOG["en"]["menu.title"]
    assert rendered == "⟦menu.title⟧"


def test_unknown_key_is_reported_for_every_language():
    for lang in LANGUAGES:
        assert t("no.such.key", lang) == "⟦no.such.key⟧"


def test_formatting_survives_a_bad_placeholder():
    """A wrong kwarg must not crash a live conversation — it degrades to the raw string."""
    assert t("signals.page", "en", wrong=1) == CATALOG["en"]["signals.page"]


# ── 3. no mixed languages ──
def test_russian_catalog_has_no_stray_english():
    offenders: dict[str, set[str]] = {}
    for key, value in CATALOG["ru"].items():
        words = {w.lower() for w in _LATIN_WORD.findall(_prose(value))}
        # HTML tag names are markup, not copy.
        words -= {"b", "code", "i", "u", "s", "a", "br", "pre"}
        stray = words - _ALLOWED_LATIN
        if stray:
            offenders[key] = stray
    assert not offenders, f"English words leaked into Russian copy: {offenders}"


def test_english_catalog_has_no_cyrillic():
    offenders = [key for key, value in CATALOG["en"].items() if _CYRILLIC.search(value)]
    assert not offenders, f"Russian text leaked into English copy: {offenders}"


def test_the_two_languages_actually_differ():
    """Guards against a locale accidentally copied from the other wholesale."""
    identical = [
        key for key in all_keys()
        if CATALOG["en"][key] == CATALOG["ru"][key]
        and _LATIN_WORD.search(_prose(CATALOG["en"][key]))
    ]
    # Emoji/symbol/placeholder-only values may legitimately match; real prose must not.
    prose = [key for key in identical if len(_prose(CATALOG["en"][key]).split()) > 3]
    assert not prose, f"Untranslated Russian entries (copied from English): {prose}"


# ── 4. no hardcoded copy ──
def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_every_key_used_in_source_exists_in_the_catalog():
    """A typo'd key would otherwise only surface as ⟦key⟧ in front of a user."""
    known = all_keys()
    unknown: dict[str, set[str]] = {}
    for path in _source_files():
        if path.parts[-2:] == ("i18n", "translator.py"):
            continue
        used = set(_T_CALL.findall(path.read_text()))
        missing = {k for k in used if k not in known}
        if missing:
            unknown[str(path.relative_to(SRC))] = missing
    assert not unknown, f"handlers reference keys absent from the catalog: {unknown}"


def test_no_cyrillic_literals_outside_the_catalog():
    """Russian copy anywhere but the catalog means a handler is holding its own text."""
    offenders: list[str] = []
    for path in _source_files():
        if path.name == "catalog.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue        # comments may discuss Russian text
            if _CYRILLIC.search(line) and ('"' in line or "'" in line):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    # The language picker names each language in its own script — intentional, and it
    # lives in the i18n layer (labels._LANGUAGE_NAMES) plus the picker keyboard, not in
    # a handler. Any OTHER file matching here is a handler holding its own copy.
    allowed = ("bot/keyboards/inline.py", "i18n/labels.py")
    offenders = [o for o in offenders if not o.startswith(allowed)]
    assert not offenders, f"hardcoded Russian outside the catalog: {offenders}"


# ── reply keyboard: matched from the catalog, not literals ──
@pytest.mark.parametrize("key", ["menu.signals", "menu.favorites",
                                 "menu.subscription", "menu.settings"])
def test_reply_keyboard_labels_match_in_every_language(key):
    labels = translations_of(key)
    assert len(labels) == len(LANGUAGES)
    for lang in LANGUAGES:
        assert t(key, lang) in labels


# ── enum labels are localized, not raw values ──
@pytest.mark.parametrize("lang", LANGUAGES)
def test_enum_labels_are_localized(lang):
    from src.i18n import arb_type_label, risk_label, status_label

    assert risk_label(RiskScore.LOW, lang) == CATALOG[lang]["risk.low"]
    assert status_label(SignalStatus.ACTIVE, lang) == CATALOG[lang]["status.active"]
    assert arb_type_label(ArbitrageType.FUNDING, lang) == CATALOG[lang]["arb.funding"]


def test_russian_risk_labels_are_not_english():
    assert risk_label_ru() != "Low"


def risk_label_ru() -> str:
    from src.i18n import risk_label

    return risk_label(RiskScore.LOW, "ru")


# ── Kazakh retirement ──
def test_kazakh_is_fully_removed():
    assert set(CATALOG) == set(LANGUAGES) == {"en", "ru"}
    assert [lang.value for lang in Language] == ["en", "ru"]
    assert not hasattr(Language, "KK")


def test_stored_kazakh_code_degrades_instead_of_crashing():
    """A replica that has not run migration 0002 must still serve the user."""
    from src.database.repositories.user_repo import _language_or_default

    assert _language_or_default("kk") is Language.RU
    assert _language_or_default("en") is Language.EN


# ── placeholder helper ──
def test_placeholders_extracts_names():
    assert placeholders("Page {current} of {total}") == {"current", "total"}
    assert placeholders("no placeholders here") == set()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
