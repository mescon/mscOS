"""mscOS modules (plugins).

The core knows playback, the signal path, the DAC and the panel. Everything that belongs to
a particular setup is a module that the user turns on under System -> Modules:

  amp          an amplifier or volume control (Mark Levinson IP, Arcam, ...); at most one
               is the volume: the Spotify slider and the panel's volume drive it
  integration  outside systems (Home Assistant) and scenes (TV mode)
  device       board or DAC extras (native DSD for C-Media CM6632A)

A module is a package here with a class derived from Plugin, named in its PLUGIN variable.
It declares its own settings (defaults), may add HTTP routes, background tasks, fields in
the panel's hello message, and reacts to core events through the hooks below. Modules
never import each other; they find one another through the hub (hub.amp, hub.plugin(id)).
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

log = logging.getLogger("plugins")


class Plugin:
    id = ""
    kind = "integration"          # amp | integration | device
    name: dict[str, str] = {}     # {"sv": ..., "en": ...}
    description: dict[str, str] = {}
    defaults: dict[str, Any] = {}  # its settings, merged into hub.settings (keys owned by the module)
    requires: tuple[str, ...] = ()  # other module kinds/ids it needs, e.g. ("amp",)

    def __init__(self, hub):
        self.hub = hub

    # ---- lifecycle
    def routes(self, routes) -> None:
        """Add HTTP routes (aiohttp RouteTableDef). Called once at start."""

    def tasks(self) -> list:
        """Background coroutines to run for the life of the panel."""
        return []

    # ---- panel
    def hello(self) -> dict:
        """Fields for the panel's hello message (its state)."""
        return {}

    def features(self) -> dict:
        """What the panel should show for this module."""
        return {}

    def update_settings(self, body: dict, upd: dict) -> None:
        """Validate this module's keys in a settings change; put accepted values in upd.
        Raise aiohttp HTTPBadRequest for a bad value. Applied by the core after all checks."""

    async def settings_applied(self, upd: dict) -> None:
        """After a settings change was saved (only called when one of its keys changed)."""

    # ---- events from the core
    async def on_play_start(self, force: bool = False) -> None:
        """Playback started on this device (or was asked for explicitly, force=True)."""

    async def notify(self, kind: str, title: str, message: str) -> bool:
        """Deliver an alert; True if delivered. Modules that are not a transport ignore it."""
        return False

    def summary(self) -> dict:
        """Fields for the compact state API (/api/ha and the like)."""
        return {}


class AmpPlugin(Plugin):
    """An amplifier (or any volume control) the core can drive. Levels are in dB."""
    kind = "amp"

    @property
    def max_db(self) -> float:
        return 40.0

    def ready(self) -> bool:
        """It answers, is on, and plays this device: the Spotify slider may move it."""
        return False

    @property
    def volume_db(self) -> float | None:
        """The last known level."""
        return None

    async def read_volume(self) -> float | None:
        return None

    async def set_volume(self, db: float, ceiling: float | None = None) -> None:
        raise NotImplementedError

    def quantize(self, db: float) -> float:
        """The level this amplifier actually takes for a request (its step size)."""
        return round(db * 2) / 2

    async def refresh(self) -> None:
        """Read its state and tell the panel."""


def discover() -> dict[str, type[Plugin]]:
    """All modules shipped in this package."""
    found: dict[str, type[Plugin]] = {}
    for m in pkgutil.iter_modules(__path__):
        if not m.ispkg:
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
        except Exception:  # a broken module must not take the panel down
            log.exception("module %s failed to load", m.name)
            continue
        cls = getattr(mod, "PLUGIN", None)
        if isinstance(cls, type) and issubclass(cls, Plugin) and cls.id:
            found[cls.id] = cls
    return found
