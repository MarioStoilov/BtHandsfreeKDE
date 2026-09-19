# BT Handsfree

A KDE Plasma tray application that turns your Linux desktop into a Bluetooth hands-free
unit for a phone (Android or iOS). Incoming calls arrive as notifications with Answer and
Reject buttons. A call in progress gets a small always-on-top window with the caller,
the running duration, Hold and Hang up buttons and a collapsible DTMF dialpad. The tray
icon opens the same menu on left and right click: the current call, speaker and
microphone levels (which open the settings window with sliders), a toggle for where
call audio plays, and the phone's battery level next to its name.

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
tracked as build step 5 in `SCOPE.md`. Until then, run from source or build the Flatpak
locally.

## Running locally in development mode

### 1. System packages

The app itself needs only Python and its wheels, but the machine has to provide the
Bluetooth and audio stack it talks to, plus the Qt platform libraries the PySide6 wheel
loads at runtime (present on any KDE Plasma desktop).

Debian / Ubuntu:

```bash
sudo apt install pipewire wireplumber libspa-0.2-bluetooth bluez bluez-obexd \
                 libxcb-cursor0 libegl1 libxkbcommon-x11-0
```

Fedora:

```bash
sudo dnf install pipewire pipewire-libs wireplumber bluez bluez-obexd \
                 xcb-util-cursor libxkbcommon-x11
```

Arch:

```bash
sudo pacman -S pipewire pipewire-audio wireplumber bluez bluez-obex xcb-util-cursor
```

The PipeWire Bluetooth plugin lives in `libspa-0.2-bluetooth` (Debian/Ubuntu),
`pipewire-libs` (Fedora) and `pipewire-audio` (Arch). `bluez-obexd` / `bluez-obex` is
only needed for the planned contacts and messages features.

### 2. uv

The project is managed with [uv](https://docs.astral.sh/uv/), which also fetches a
suitable Python if the system one is older than 3.12:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

or `pipx install uv`, or a distribution package where one exists (`sudo dnf install uv`,
`sudo pacman -S uv`; Ubuntu has none).

### 3. Check the stack before the first run

Pair and connect the phone, then confirm PipeWire took the hands-free role and publishes
the telephony service; the reply should list one gateway object per connected phone:

```bash
bluetoothctl devices Connected
busctl --user call org.pipewire.Telephony /org/pipewire/Telephony \
       org.freedesktop.DBus.ObjectManager GetManagedObjects
```

An empty reply or `Name ... was not provided by any .service files` means PipeWire is
older than 1.4, is configured with the oFono backend, or the phone is connected with
audio only (re-connect it from the phone's Bluetooth settings with call audio enabled).

### 4. Install and run

```bash
git clone git@github.com:MarioStoilov/BtHandsfreeKDE.git
cd BtHandsfreeKDE
uv sync                                  # creates .venv with PySide6, dbus-fast, qasync, ruff
uv run bt-handsfree-kde --verbose        # --verbose logs every D-Bus event
```

The tray icon appears in the Plasma system tray. Quit from the tray menu or with
Ctrl+C in the terminal.

Optional: install the desktop file and icon for your user so the notification server
and the desktop portal can associate the running app with its entry (silences the
"Could not register app ID" warning on start):

```bash
install -Dm644 data/io.github.MarioStoilov.BtHandsfreeKDE.desktop \
        -t ~/.local/share/applications
install -Dm644 src/bt_handsfree_kde/resources/icon.svg \
        ~/.local/share/icons/hicolor/scalable/apps/io.github.MarioStoilov.BtHandsfreeKDE.svg
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
| `--socket=pulseaudio` | Dialpad key tones (call audio itself never passes through the app) |

No network or filesystem access is requested.

## Development

```bash
uv run ruff format && uv run ruff check
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest flatpak/io.github.MarioStoilov.BtHandsfreeKDE.yml
busctl --user call org.pipewire.Telephony /org/pipewire/Telephony org.freedesktop.DBus.ObjectManager GetManagedObjects
```

Coding standards and repository rules are in `CLAUDE.md`.

## License

MIT, see `LICENSE`.
