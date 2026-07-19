"""Package dependency direction is enforced, not just documented.

The intended layering is::

    bot  →  services  →  i18n  →  domain / config

This test exists because that arrangement already broke once: localization lived under
``src/bot/i18n``, and when the daily-summary and subscription-expiry notices needed
translating, ``services`` had to import from ``bot`` to get it. Nothing failed — Python
resolved the cycle because ``bot.i18n`` happened not to import the rest of ``bot`` — so
the inversion was invisible until someone read the imports.

A static check is the only thing that catches that class of regression at review time.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

#: layer -> packages it is allowed to import from (plus itself and stdlib/third-party).
_ALLOWED: dict[str, set[str]] = {
    "config": set(),
    "domain": {"config"},
    "i18n": {"domain", "config"},
    "database": {"domain", "config"},
    "scanner": {"domain", "config", "database", "services"},
    "services": {"domain", "config", "database", "i18n", "scanner"},
    "bot": {"domain", "config", "database", "i18n", "services", "scanner"},
}


#: Known, deliberate exceptions — recorded rather than silently permitted, so each one
#: stays visible in review. Keyed by module path; the value is the layer it may import.
_EXCEPTIONS: dict[str, set[str]] = {
    # settings.validate() asserts the dev paywall bypass is off in production. The import
    # is function-local precisely to avoid a module-level config↔domain cycle, so it
    # cannot deadlock imports; it is coupling, not a cycle. Pre-dates the i18n move.
    "config/settings.py": {"domain"},
}


def _layer_of(path: Path) -> str | None:
    rel = path.relative_to(SRC).parts
    return rel[0] if len(rel) > 1 or rel[0] != "app.py" else None


def _imported_layers(path: Path) -> set[str]:
    """First-party ``src.<layer>`` packages this module imports."""
    tree = ast.parse(path.read_text(), filename=str(path))
    layers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            parts = node.module.split(".")
            if len(parts) >= 2 and parts[0] == "src":
                layers.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[0] == "src":
                    layers.add(parts[1])
    return layers


def _modules() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in p.parts]


def test_no_layer_imports_above_itself():
    violations: list[str] = []
    for path in _modules():
        layer = _layer_of(path)
        if layer is None or layer not in _ALLOWED:
            continue        # src/app.py is the composition root; it may import anything
        rel = str(path.relative_to(SRC))
        allowed = _ALLOWED[layer] | {layer} | _EXCEPTIONS.get(rel, set())
        for imported in _imported_layers(path) - allowed:
            violations.append(f"{path.relative_to(SRC)} ({layer}) imports src.{imported}")
    assert not violations, "dependency direction violated:\n  " + "\n  ".join(violations)


def test_services_never_imports_bot():
    """The specific inversion this layering was refactored to remove."""
    offenders = [
        str(p.relative_to(SRC)) for p in _modules()
        if _layer_of(p) == "services" and "bot" in _imported_layers(p)
    ]
    assert not offenders, f"services imports bot again: {offenders}"


def test_i18n_depends_only_on_domain_and_config():
    """i18n is the shared leaf — anything heavier here would drag the bot into services."""
    for path in _modules():
        if _layer_of(path) != "i18n":
            continue
        assert _imported_layers(path) <= {"i18n", "domain", "config"}, path.name


def test_i18n_does_not_import_aiogram():
    """Framework independence is what lets services translate without pulling in the bot."""
    for path in _modules():
        if _layer_of(path) == "i18n":
            assert "aiogram" not in path.read_text(), path.name


@pytest.mark.parametrize("first", [
    "src.i18n", "src.services.scheduler", "src.services.notification_service",
    "src.bot.handlers", "src.app",
])
def test_no_circular_imports_regardless_of_entry_point(first):
    """A cycle can hide behind import order — importing each entry point first in a
    clean interpreter is what actually proves there isn't one."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True, text=True,
        cwd=str(SRC.parent),
    )
    assert result.returncode == 0, f"importing {first} first failed:\n{result.stderr}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
