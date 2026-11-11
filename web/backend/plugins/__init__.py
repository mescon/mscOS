"""mscOS modules (plugins).

The core knows playback, the signal path, the DAC and the panel. Everything that belongs to
a particular setup is a module that the user turns on under System -> Modules:

  source       a player that owns the DAC, one at a time: Spotify (Soloist or librespot),
               Roon Bridge, HQPlayer NAA, ...; the source switch lists the enabled ones
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
    kind = "integration"          # source | amp | integration | device
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
    model = ""  # the default name of what this device plays into, e.g. "Mark Levinson Nº585.5"

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


class SourcePlugin(Plugin):
    """A player. Only one source runs at a time (it owns the DAC); the core starts and stops
    them through their systemd units. The software itself is installed on the device by the
    source's install script (<lib>/sources/<id>/install.sh), from its publisher: mscOS ships
    no player binaries and no keys."""
    kind = "source"
    label = ""                         # on the source switch, e.g. "Roon"
    protocol = ""                      # tag in the signal chain, e.g. "RAAT"
    pipewire = True                    # plays through PipeWire; False: opens the DAC itself
    user_units: tuple[str, ...] = ()   # systemd --user units of the audio user
    system_units: tuple[str, ...] = ()
    hint: dict[str, str] = {}          # what the listener does to play, e.g. choose it in the app
    terms: dict[str, str] = {}         # shown before installing (licence, account needed)
    terms_url = ""

    async def installed(self) -> bool:
        return False

    async def active(self) -> bool:
        import collect
        if self.user_units:
            return await collect.units_active(self.user_units, user=True)
        return await collect.units_active(self.system_units, user=False)

    async def _ctl(self, verb: str) -> str:
        import collect
        err = ""
        rev = verb == "stop"
        if self.system_units:
            u = list(reversed(self.system_units)) if rev else list(self.system_units)
            rc, _, e = await collect.run(["systemctl", verb, *u], timeout=60)
            err += e if rc else ""
        if self.user_units:
            u = list(reversed(self.user_units)) if rev else list(self.user_units)
            rc, _, e = await collect.run(["systemctl", "--user", verb, *u], as_user=True, timeout=60)
            err += e if rc else ""
        return err.strip()

    async def start(self) -> str:
        """Start playing software; returns an error text, empty when it went well."""
        return await self._ctl("start")

    async def stop(self) -> str:
        return await self._ctl("stop")

    async def playing(self) -> bool | None:
        """Is it playing right now? None: not known here (the core then looks at the DAC)."""
        return None

    def now_playing(self) -> dict | None:
        """What plays, for the panel: title, artist, album, image, format. None: not known."""
        return None

    async def state(self) -> dict:
        """Extra facts for the panel's status (version, ...)."""
        return {}


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
