# BT Handsfree

A KDE Plasma tray application that turns your Linux desktop into a Bluetooth hands-free
unit for a phone (Android or iOS). Incoming calls arrive as notifications with Answer and
Reject buttons; a call in progress shows caller and duration with Hold and Hang up; the
tray menu adds volume control, a choice of where call audio plays, and the phone's
battery level next to its name.

Planned and in progress: dialpad with `tel:` link handling, contacts sync, messages.
The full feature list, how each maps onto the Bluetooth stack, and the known
shortcomings are in [`SCOPE.md`](SCOPE.md).

## Requirements

- PipeWire 1.4 or newer with the native Bluetooth HFP backend (the default; not oFono)
  and its telephony D-Bus service enabled (default). The app is a client of
  `org.pipewire.Telephony`; it never touches audio itself.
- BlueZ. Contacts and messages (planned) additionally need obexd (`bluez-obexd`).
- A desktop with a StatusNotifierItem tray and a freedesktop notification server.
  Developed and tested on KDE Plasma 6.
- Your phone paired and connected with the hands-free profile.

Known shortcoming: no cellular reception indicator. PipeWire receives the value from the
phone but does not publish it on D-Bus, so the tray shows battery only. See `SCOPE.md`.

## Install

Flatpak on Flathub is the intended distribution channel; the first submission is
tracked as build step 5 in `SCOPE.md`. Until then, build the Flatpak locally or run
from source.

### Run from source

```bash
uv sync
uv run bt-handsfree-kde            # add --verbose for D-Bus level logging
```

### Local Flatpak build

```bash
sudo apt install flatpak flatpak-builder            # Debian/Ubuntu
flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak-builder --user --install --force-clean build-dir flatpak/io.github.MarioStoilov.BtHandsfreeKDE.yml
flatpak run io.github.MarioStoilov.BtHandsfreeKDE
```

The manifest builds from the working tree (`type: dir`). The Flathub copy pins a git tag
and commit instead.

## Sandbox permissions

| Permission | Why |
| --- | --- |
| `--talk-name=org.pipewire.Telephony` | Call state and call control from PipeWire |
| `--talk-name=org.kde.StatusNotifierWatcher` | Tray icon |
| `--talk-name=org.freedesktop.Notifications` | Call notifications with buttons |
| `--system-talk-name=org.bluez` | Phone name, connection state, battery |
| `--socket=wayland`, `--socket=fallback-x11`, `--share=ipc` | Windows |

No audio, network or filesystem access is requested.

## Development

```bash
uv run ruff format && uv run ruff check
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest flatpak/io.github.MarioStoilov.BtHandsfreeKDE.yml
busctl --user call org.pipewire.Telephony /org/pipewire/Telephony org.freedesktop.DBus.ObjectManager GetManagedObjects
```

Coding standards and repository rules are in `CLAUDE.md`.

## License

MIT, see `LICENSE`.
