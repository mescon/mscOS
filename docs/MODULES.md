# Modules

The core of mscOS knows playback, the signal path, the DAC and the panel. Everything that
belongs to a particular setup is a module, turned on under **Modules**:

| Kind | What | Shipped |
|---|---|---|
| `source` | a player that owns the DAC, one at a time | `spotify` (Soloist, Lossless), `librespot` (go-librespot), `roon`, `naa` |
| `amp` | an amplifier or other volume control; at most one | `levinson` (Mark Levinson Nº585/585.5) |
| `device` | board or DAC extras | `cm6632a` (native DSD) |
| `integration` | outside systems and scenes | `homeassistant`, `arcam`, `tvmode` |

A module is a Python package in `web/backend/plugins/<id>/` whose `__init__.py` defines a
class derived from `Plugin` (or `SourcePlugin`, `AmpPlugin`) and names it in `PLUGIN`. The
base classes in `web/backend/plugins/__init__.py` document every hook; in short:

```python
from plugins import Plugin

class Example(Plugin):
    id = "example"
    kind = "integration"
    name = {"sv": "Exempel", "en": "Example"}
    description = {"sv": "Vad modulen gör.", "en": "What the module does."}
    defaults = {"example_host": ""}            # its settings, stored with the panel's
    options = ({"key": "example_host", "type": "text",
                "label": {"sv": "Adress", "en": "Address"}},)   # shown under the module

    def update_settings(self, body, upd):      # validate a change; raise HTTPBadRequest if bad
        if "example_host" in body:
            upd["example_host"] = str(body["example_host"]).strip()

    def routes(self, routes):                  # its own HTTP API
        ...
    def tasks(self):                           # background coroutines
        return []
    async def on_play_start(self, force=False):  # playback started on this device
        ...
    def status(self):                          # one line in the Modules view
        return {"state": "ok", "text": {"sv": "Ansluten.", "en": "Connected."}}

PLUGIN = Example
```

Modules never import each other. They reach the rest through the hub: `hub.settings`,
`hub.amp` (the amplifier module, if any), `hub.source` (the current source),
`hub.plugin(id)`, `hub.broadcast(message)` to the panel.

## Sources

A source declares the systemd units of its player and whether it plays through PipeWire
(`pipewire = True`: Spotify) or opens the DAC itself (`pipewire = False`: Roon, HQPlayer
NAA, needed for DSD and for sample rates other than 44.1 kHz). The core starts and stops
the units; it stops every other source first, and PipeWire too when the new source wants
the DAC for itself.

The player itself is not part of mscOS. `sources/<id>/install.sh` downloads it on the
device from its publisher, and the panel runs that script when the user presses Install
(the module's `terms` text is shown first). Units are never enabled at boot: the panel
starts the chosen source.

A source that knows what it plays implements `now_playing()` and calls
`hub.source_changed(self)` when that changes (see `librespot`).

## Amplifiers

An `AmpPlugin` gives the core a volume in dB: `ready()`, `volume_db`, `read_volume()`,
`set_volume()`, `quantize()`. With one enabled, the Spotify volume slider drives the
amplifier while the stream stays at full scale, so the samples are never recalculated.

## Texts

A module carries its own names and descriptions as `{"sv": ..., "en": ...}`. Error
messages go through `T("svensk text {värde}", värde=...)` from `i18n`, which looks the
Swedish text up in `web/static/i18n/<language>.json`.

## What a module gives the panel

A module needs no page of its own. The panel draws these from what the module returns:

| Method | Shown as |
|---|---|
| `options` / `option_list()` | its settings in its card under Modules: `text`, `number`, `select`, `toggle`, `secret` |
| `status()` | one line in its card: connected, not set up, ... |
| `log()` | recent communication, folded, for troubleshooting |
| `panel()` and `panel_action()` | a card of buttons, sliders and choices in the Listen view |
| `amp_state()` and `amp_action()` (an `AmpPlugin`) | the volume dial, inputs, power and mute |
| `now_playing()`, `transport` and `command()` (a `SourcePlugin`) | what plays, and the transport buttons |
| `notify()` and `can_notify()` | a way to deliver the core's alerts to a phone |

A `secret` option (an address with a token, a key) is stored in a root-only file on the
device, never in the settings and never sent back to the panel; read it with
`self.hub.secret(key)`.

## Modules of your own

A module does not have to come with mscOS. Put its package in
`/var/lib/mscos/plugins/<id>/` on the device (owned by root, writable by root only), restart
the panel and turn it on under Modules. [`examples/module-hello`](../examples/module-hello)
is a complete small one.

To let others install it from the panel (Modules, Add a module), publish two files over
HTTPS: the package as a tar archive, and a `module.json` that names it:

```json
{"id": "mymodule", "version": "1.2.0",
 "archive": "https://example.org/mymodule-1.2.0.tar.gz", "sha256": "<of the archive>"}
```

The user pastes the address of that `module.json`. The panel remembers it, and "Check for
updates" looks there for a newer version. A module runs inside the panel as root: there is
no sandbox, and the panel says so before installing one.
