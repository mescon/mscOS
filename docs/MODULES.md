# Modules

The core of mscOS knows playback, the signal path, the DAC and the panel. Everything that
belongs to a particular setup is a module, turned on under **Modules**:

| Kind | What | Shipped |
|---|---|---|
| `source` | a player that owns the DAC, one at a time | `spotify` (Soloist, Lossless), `librespot` (go-librespot), `roon`, `naa` |
| `amp` | an amplifier or other volume control; at most one | `levinson` (Mark Levinson Nº585/585.5) |
| `device` | board or DAC extras | `cm6632a` (native DSD) |
| `notify` | a way to deliver alerts to a phone | `ntfy` |
| `integration` | outside systems and scenes | `homeassistant`, `arcam`, `tvmode` |

A module is a directory [`modules/<id>/`](../modules): a `module.json` (id, version, kind,
name, description) and a Python package whose `__init__.py` defines a class derived from
`Plugin` (or `SourcePlugin`, `AmpPlugin`) and names it in `PLUGIN`. The base classes in
`web/backend/plugins/__init__.py` document every hook; in short:

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

The player itself is not part of mscOS. `modules/<id>/install.sh` downloads it on the
device from its publisher, and the panel runs that script when the user presses Install
(the module's `terms` text is shown first). Units are never enabled at boot: the panel
starts the chosen source.

A source that knows what it plays implements `now_playing()` and calls
`hub.source_changed(self)` when that changes (see `librespot`). One that can be controlled
lists what it takes in `transport` (toggle, play, pause, next, prev, seek, shuffle, repeat)
and does it in `command()`: the panel's buttons then work for it. One whose app has a
volume slider implements `slider()` and `set_slider()`, and the volume bridge makes that
slider the amplifier's remote control.

The core knows nothing about any one player. What a source needs done regularly or at
certain moments it gets through hooks: `tick()` every five seconds, `check_health()` for
its own alerts, `play_began()`, `lost_dac()`, `status_fields()` for the status the panel
shows, `optimization_rows()` for its own measures in the optimization list, and
`save_state()` for small state kept across restarts. `spotify` uses all of them.

## Amplifiers

An `AmpPlugin` gives the core a volume in dB: `ready()`, `volume_db`, `read_volume()`,
`set_volume()`, `quantize()`. With one enabled, the Spotify volume slider drives the
amplifier while the stream stays at full scale, so the samples are never recalculated.

The panel's volume dial, input list, power and mute are drawn from `amp_state()` and act
through `amp_action()`: an amplifier module needs no page of its own (see `levinson`).
Two safety rules are the bridge's, not the module's: one move of the slider raises the
amplifier by 6 dB at most, and for some seconds after the device is selected or playback
starts the slider follows the amplifier, never the other way.

## Tests

`tools/test.sh` runs the tests in a container: every module's manifest and texts, the
catalogue, archives of modules from elsewhere, the volume bridge, and the HTTP API of a new
installation. They need no device. Add a test with your module if it has logic worth one.

## Texts

A module carries its own names and descriptions as `{"sv": ..., "en": ...}`. Error
messages go through `T("svensk text {värde}", värde=...)` from `i18n`, which looks the
Swedish text up in `web/static/i18n/<language>.json`.

## What a module gives the panel

A module needs no page of its own (it may bring page code, see below). The panel draws these from what the module returns:

| Method | Shown as |
|---|---|
| `options` / `option_list()` | its settings in its card under Modules: `text`, `number`, `select`, `toggle`, `secret` |
| `status()` | one line in its card: connected, not set up, ... |
| `log()` | recent communication, folded, for troubleshooting |
| `facts()` | rows of state in its card (service running, signed in, ...) |
| `actions` and `action()` | buttons in its card (restart the player, look for a new version) |
| `panel()` and `panel_action()` | a card of buttons, sliders and choices in the Controls view |
| `amp_state()` and `amp_action()` (an `AmpPlugin`) | the volume dial, inputs, power and mute |
| `now_playing()`, `transport` and `command()` (a `SourcePlugin`) | what plays, and the transport buttons |
| `notify()` and `can_notify()` | a way to deliver the core's alerts to a phone |

A `secret` option (an address with a token, a key) is stored in a root-only file on the
device, never in the settings and never sent back to the panel; read it with
`self.hub.secret(key)`.

A module with alerts of its own lists them in `alert_kinds` (`{"id": ..., "label": {"sv": ...,
"en": ...}}`); they appear under Profile, where each can be switched off, and are sent with
`self.hub.alert(id, title, text)`.

## Page code

Most modules need none. A module that wants to draw more than the table above allows puts a
`page.js` beside its `__init__.py`. The panel loads it while the module is on. Its default
export receives `api` and returns the hooks it wants, all optional:

```js
export default function page(api) {
  const { $, S, tr, esc, post } = api;
  api.slot("play", `<details class="fold" data-k="mine"><summary><span>${tr("Mitt")}</span></summary>…</details>`);
  return {
    hello(m) {},        // the first message on the socket, with what the module's hello() returned
    message(m) {},      // every later message; return true when it was this module's alone
    render() {},        // after every change: show or hide the module's own parts
    issues() { return []; },  // texts for "Needs attention" at the top of System
    // a source, while it is the chosen one, may replace the general views:
    play() { return { title, artist, album, cover, format: { text, lossy, pending }, playing, shuffle, repeat }; },
    position() {}, duration() {}, playing() {},   // milliseconds, milliseconds, true or false
    track() { return { title, artist, album, artist_uri }; },  // for modules that look the music up; artist_uri when the player has one
    chain() { return { stages, running, playing, rate, issues, dac, seal: { head, sub, cls }, bits, gain }; },
    carry(format) {},   // what an ALSA format really carries, in words
  };
}
```

`api` holds the panel's state (`S`: `status`, `settings`, `features`, `names`, `xruns`), its
helpers (`$`, `$$`, `tr`, `esc`, `dec`, `khz`, `fmtWord`, `fmtTime`, `clock`, `stamp`, `post`,
`toast`, `fv`, `loc`, `MARK`, `outName`, `srcId`, `srcState`), `lang()` (the page's language),
`track()` (what is playing, whichever source plays it: `{title, artist, album}` or `null`),
`render()` to draw the Listen and Signal views again, and `slot(where, html)`, which puts the
module's own markup on the page: `"notice"` and `"play"` beside the sleeve and `"about"` under it
in the Listen view, `"signal"` in the Signal view. The markup is removed when the module is
switched off; a `<style>` element inside it carries the module's own styles. Everything that does not come from the module itself goes
through `esc()` before it is put in markup.

Texts are written in Swedish through `tr("…")` or ``tr`…${value}…` ``; the other languages are
in `i18n/<language>.json` beside the module (`tools/i18n-keys.py` lists what is missing).
`modules/spotify/page.js` is the worked example of a source: the queue, the measured format, the
signal chain and the verification are all drawn there. `modules/about/page.js` is a small one
that is no source: it reads `api.track()` and shows notes on the music under the sleeve.

A module's card (`panel()`, above) is shown in the Controls view, with the amplifier's input and
power. The Listen view is for the music.

## The catalogue

`modules/index.json` lists every module in the repository with its version and the checksum
of each file (`tools/module-index.py` writes it). The panel reads it from the project's
repository when the user presses Browse the catalogue or Check for updates, and can install
a module that is new, or newer than the one that came with the installed mscOS. Such a
module is kept in `/var/lib/mscos/plugins/` and replaces the shipped one only while it is
newer; removing it goes back to the shipped one. `MSCOS_CATALOGUE` in `/etc/mscos/site.env`
points the panel at another catalogue (a fork, a mirror).

The catalogue is signed (`modules/index.json.sig`), and a device refuses one that is not: a
module runs as root, and the catalogue names every file with its checksum. A pull request
changes `index.json` but cannot sign it. The maintainer signs after reviewing and merging
(`tools/sign.sh catalogue`), and until then devices keep the modules they have. See
[INSTALL.md](INSTALL.md#signatures) for the keys and for forks.

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
