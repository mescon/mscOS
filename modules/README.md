# Modules

Everything that belongs to a particular setup is a module: a player (source), an amplifier,
a way to send alerts, an integration with another system. Each directory here is one module.
They all come with mscOS, and the panel can also install a newer or a new one from this
directory without waiting for the next mscOS release (Modules, Browse the catalogue).

## Contributing a module

1. Copy [`examples/module-hello`](../examples/module-hello) to `modules/<id>/`. The id is
   the directory's name: lower-case letters, digits and `_`.
2. Write the module: a class derived from `Plugin` (or `SourcePlugin`, `AmpPlugin`) named in
   `PLUGIN`. [docs/MODULES.md](../docs/MODULES.md) lists what a module can give the panel;
   the base classes in `web/backend/plugins/__init__.py` document every hook. A module needs
   no page of its own: the panel draws its options, status, cards and controls.
3. Fill in `module.json`: `id`, `version`, `kind` (`source`, `amp`, `device`, `notify` or
   `integration`), and `name` and `description` in Swedish and English.
4. A source that needs a player installed adds `install.sh` (see `librespot/`). It downloads
   the player from its publisher onto the device: no player binaries and no keys in this
   repository.
5. Run `tools/module-index.py` and commit the updated `index.json` with your module.
6. Open a pull request.

Rules: no secrets, addresses or names from your own installation (a module's settings are
entered in the panel; secrets use the `secret` option type). Texts the user sees are given in
both languages. Raise `version` in `module.json` whenever the module's behaviour changes, or
installed devices will not see the update.

A module runs inside the panel, as root. Pull requests are reviewed with that in mind.
