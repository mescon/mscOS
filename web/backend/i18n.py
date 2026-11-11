"""Translation of texts the backend writes: error messages, the optimizations list, alerts.

Swedish is the source language, as in the panel (static/i18n.js): T("Fel lösenord.") looks
the Swedish text up in static/i18n/<lang>.json, the same file the panel uses, and a text
missing from it stays Swedish. Values go in after the lookup: T("{n} avbrott", n=3).

The language is the one of the request being answered: the panel stores its language in the
"lang" cookie, otherwise the browser's Accept-Language decides. Texts written without a
request (alerts to the phone) use the system language, the "language" setting.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os

log = logging.getLogger("i18n")
LANGS = ("sv", "en")
_lang: contextvars.ContextVar[str | None] = contextvars.ContextVar("lang", default=None)
_dicts: dict[str, dict[str, str]] = {"sv": {}}
_dir = ""
system_language = lambda: "en"  # noqa: E731 - set by the hub (the "language" setting)


def setup(static_dir: str, system_lang) -> None:
    global _dir, system_language
    _dir, system_language = os.path.join(static_dir, "i18n"), system_lang


def _dict(lang: str) -> dict[str, str]:
    if lang not in _dicts:
        try:
            with open(os.path.join(_dir, f"{lang}.json"), encoding="utf-8") as f:
                _dicts[lang] = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("no translation for %s: %s", lang, e)
            _dicts[lang] = {}
    return _dicts[lang]


def from_request(request) -> str:
    """The language of a request: the panel's cookie, else the browser's preference."""
    c = request.cookies.get("lang")
    if c in LANGS:
        return c
    for part in request.headers.get("Accept-Language", "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
        if code:
            return "en"  # any other language reads English
    return system_language()


def use(lang: str | None):
    """Set the language for the current task (a request); returns the reset token."""
    return _lang.set(lang if lang in LANGS else None)


def current() -> str:
    return _lang.get() or system_language()


def T(text: str, **values) -> str:
    out = _dict(current()).get(text, text)
    return out.format(**values) if values else out


def decimal(x, digits: int = 1) -> str:
    """44,1 in Swedish, 44.1 otherwise."""
    s = f"{x:.{digits}f}"
    return s.replace(".", ",") if current() == "sv" else s


def N_(text: str) -> str:
    """Marks a text for translation where it is written; T() translates it where it is shown."""
    return text
