"""mscOS modules (plugins).

The core knows playback, the signal path, the DAC and the panel. Everything that belongs to
a particular setup is a module that the user turns on under System -> Modules:

  source       a player that owns the DAC, one at a time: Spotify (Soloist or librespot),
               Roon Bridge, HQPlayer NAA, ...; the source switch lists the enabled ones
  amp          an amplifier or volume control (Mark Levinson IP, Arcam, ...); at most one
               is the volume: the Spotify slider and the panel's volume drive it
  notify       a way to deliver alerts to a phone (ntfy, ...)
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
    kind = "integration"          # source | amp | device | notify | integration
    name: dict[str, str] = {}     # {"sv": ..., "en": ...}
    description: dict[str, str] = {}
    defaults: dict[str, Any] = {}  # its settings, merged into hub.settings (keys owned by the module)
    requires: tuple[str, ...] = ()  # other module kinds/ids it needs, e.g. ("amp",)
    external: dict | None = None    # set for a module installed beside mscOS: its module.json
    # Settings the panel shows under the module (Modules view) and saves through /api/settings:
    #   {"key": ..., "type": "text" | "number" | "select" | "toggle" | "secret",
    #    "label": {sv, en}, "help": {sv, en}, "options": [[value, {sv, en}], ...] (select),
    #    "placeholder": ..., "min"/"max"/"step"/"unit" (number)}
    # A "secret" (an address with a token, a key) is kept in the root-only secrets file,
    # never in the settings and never sent to the panel: hub.secret(key) reads it.
    options: tuple[dict, ...] = ()

    def option_list(self) -> list[dict]:
        """The options as shown now; a module whose choices depend on other modules overrides it."""
        return list(self.options)

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

    def can_notify(self) -> bool:
        """Is this module a way to deliver alerts, and set up?"""
        return False

    def summary(self) -> dict:
        """Fields for the compact state API (/api/ha and the like)."""
        return {}

    async def tick(self) -> None:
        """Every five seconds, with the core's sampling: light periodic work."""

    async def check_health(self) -> None:
        """With the core's health check (every 5 s with a panel open, else once a minute):
        raise the module's own alerts through hub.alert."""

    def save_state(self) -> dict:
        """Small state to keep across restarts, merged into the core's stats file (written
        rarely). Read it back in __init__ from hub.saved_stats."""
        return {}

    def clear_history(self) -> None:
        """The user cleared the history (dropouts, verification log)."""

    def status(self) -> dict | None:
        """One line for the Modules view: {"state": "ok" | "warn" | "idle", "text": {sv, en}}."""
        return None

    def log(self) -> list[str] | None:
        """Recent lines of its communication, for troubleshooting in the Modules view."""
        return None

    # ---- a card of its own in the Listen view
    def panel(self) -> dict | None:
        """Controls the panel draws without knowing the module:
          {"title": {sv, en}, "summary": {sv, en}, "lead": {sv, en}, "controls": [
             {"type": "button", "name", "label": {sv, en}, "value": what a press sends, "pressed": bool},
             {"type": "range", "name", "label", "min", "max", "step", "value", "unit"},
             {"type": "choice", "name", "label", "options": [[value, label], ...], "value"}]}
        None: no card."""
        return None

    async def panel_action(self, name: str, value) -> None:
        """A control was used. name "refresh": the card was opened, read the device now.
        Raise RuntimeError with a text for the user when it cannot be done."""


class AmpPlugin(Plugin):
    """An amplifier (or any volume control) the core can drive. Levels are in dB.

    The panel's volume dial, input list, power and mute are drawn from amp_state() and act
    through amp_action() (POST /api/amp): an amplifier module needs no page of its own."""
    kind = "amp"
    model = ""  # the default name of what this device plays into, e.g. "Mark Levinson Nº585.5"
    # the Spotify slider as the amplifier's remote control is the core's (the volume bridge)
    BRIDGE_OPTION = {"key": "spotify_vol_to_amp", "type": "toggle",
                     "label": {"sv": "Spotify-reglaget styr förstärkarens volym", "en": "The Spotify slider drives the amplifier's volume"},
                     "help": {"sv": "Samplen lämnas orörda, så signalen förblir bitperfekt.", "en": "The samples are left untouched, so the signal stays bit-perfect."}}

    def amp_state(self) -> dict:
        """{"reachable": bool | None (None: not known yet), "power": bool | None, "muted": bool,
        "volume_db": float | None, "max_db": the panel's ceiling, "input": id,
        "inputs": [{"id", "label"}], "note": {sv, en} when it cannot be controlled (why)}"""
        return {"reachable": None}

    async def amp_action(self, b: dict) -> None:
        """power: bool, mute: bool, volume_db: float, volume_step_db: float, input: id.
        Raise aiohttp HTTP errors with a text for the user."""

    def hello(self) -> dict:
        return {"amp": self.amp_state()}

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
    has_slider = False                 # its app has a volume slider the bridge can use: slider()
    pushes_playing = False             # it tells the core itself when playback starts and stops
                                       # (hub._set_playing); otherwise the core asks playing()
    # What the panel's buttons may ask of the player, through command():
    #   toggle, play, pause, next, prev, seek (position_ms), shuffle (enabled), repeat (mode: off | context | track)
    transport: tuple[str, ...] = ()
    updates = "manual"                 # "self": the player keeps itself up to date; "manual":
                                       # the panel looks for a newer one and runs the install script again

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
        """What plays, for the panel: title, artist, album, image, playing, duration_ms,
        position_ms with position_ts (when it was read, epoch seconds), codec, bitrate,
        shuffle, repeat (off | context | track). None: not known."""
        return None

    async def state(self) -> dict:
        """Extra facts for the panel's status (version, ...)."""
        return {}

    def lost_dac(self) -> None:
        """Another source was chosen: forget what was playing."""

    def play_began(self) -> None:
        """Playback started here after a pause (not a track change)."""

    async def command(self, cmd: str, **kw) -> None:
        """One of its transport commands. Raise RuntimeError with a text for the user."""
        raise RuntimeError("no transport")

    async def names_changed(self) -> None:
        """The device's names changed (hub.names()): take the new name on the network."""

    async def version(self) -> str | None:
        """The installed player's version, None if not known."""
        return None

    async def latest(self) -> str | None:
        """The newest version at the publisher (asks the publisher's site). None: not known."""
        return None

    def recorded_version(self) -> str | None:
        """What the install script noted in <state>/versions/<id>, for players that cannot
        tell their own version."""
        import collect
        v = collect.read(f"{collect.STATE_DIR}/versions/{self.id}").strip()
        return v or None

    def slider(self) -> dict | None:
        """The volume slider in the source's own app (Spotify), for the volume bridge:
        {"active": this device is the one selected, "volume": 0-100 or None, "digital": the
        slider scales the samples}. None: no slider, or it is switched off."""
        return None

    async def set_slider(self, pct: int) -> None:
        """Move that slider (the amplifier was changed elsewhere)."""


SHIPPED: set[str] = set()  # ids of the modules that come with mscOS (filled by discover)


def _discover_external(found: dict) -> None:
    """Modules installed beside mscOS (<state>/plugins/<id>): see extmods.py. A shipped
    module is never replaced by one of these."""
    import importlib.util
    import os
    import sys
    import extmods
    if not os.path.isdir(extmods.DIR):
        return
    for name in sorted(os.listdir(extmods.DIR)):
        path = os.path.join(extmods.DIR, name)
        if name.startswith(".") or not os.path.isfile(os.path.join(path, "__init__.py")) or name in found:
            continue
        if not extmods.safe_dir(path):
            log.warning("module %s is not loaded: its files must belong to root and be writable by root only", name)
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"mscos_ext_{name}", os.path.join(path, "__init__.py"),
                                                          submodule_search_locations=[path])
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        except Exception:  # a broken module must not take the panel down
            log.exception("module %s failed to load", name)
            continue
        cls = getattr(mod, "PLUGIN", None)
        if isinstance(cls, type) and issubclass(cls, Plugin) and cls.id == name:
            cls.external = extmods.manifest_of(path) or {"id": name, "version": ""}
            found[cls.id] = cls
        else:
            log.warning("module %s: PLUGIN must be a Plugin class whose id is the directory's name", name)


def discover() -> dict[str, type[Plugin]]:
    """All modules: those shipped in this package, then the ones installed beside mscOS."""
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
    SHIPPED.update(found)
    try:
        _discover_external(found)
    except Exception:  # noqa: BLE001
        log.exception("modules beside mscOS")
    return found
