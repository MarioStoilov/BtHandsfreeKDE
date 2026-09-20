# BT Handsfree

A KDE Plasma tray application that turns your Linux desktop into a Bluetooth hands-free
unit for a phone. Incoming calls arrive as notifications with Answer and
Reject buttons. A call in progress gets a small always-on-top window with the caller,
the running duration, Hold and Hang up buttons and a collapsible DTMF dialpad. The main
window has a Dialpad tab and a Contacts tab. The dialpad places calls; clicking a `tel:`
link anywhere on the desktop opens it with the number filled in, and while a call is up
its keys send DTMF tones. The Contacts tab lists the phone's contacts, read over
Bluetooth each time the phone connects, with search and a Call button; the same list
supplies caller names for incoming calls, which the hands-free profile itself does not
carry. The Messages tab shows the newest SMS conversations from the phone; a new
message arrives as a notification with an Open button, and opening a conversation marks
it read on the phone. The tray icon opens the same menu on left and right click: the
current call, speaker and microphone levels (which open the settings window with
sliders), a toggle for where call audio plays, the dialpad, contacts and messages, and
the phone's battery level next to its name.

Planned: sending messages.
The full feature list, how each maps onto the Bluetooth stack, and the known
shortcomings are in [`SCOPE.md`](SCOPE.md).

> **Tested on Android only.** Everything here was developed and verified against one
> Android phone (Samsung). iOS should work over the same Bluetooth profiles but has not
> been tried; caller names, the contact and message permission prompts and the message
> limits described in `SCOPE.md` are expected to differ.
>
> **Full disclosure:** this is a vibe-coded app. I wanted my phone's calls on my
> desktop and had an AI assistant write it with me, step by step, against a real phone.
> It works for me. It may work for you too — but set your expectations accordingly.

## Requirements

- PipeWire 1.4 or newer with the native Bluetooth HFP backend (the default; not oFono)
  and its telephony D-Bus service enabled (default). The app is a client of
  `org.pipewire.Telephony`; it never touches audio itself.
- BlueZ, with obexd (`bluez-obexd`) for contacts and messages. Without obexd, calls
  work and those two tabs say what is missing.
- A desktop with a StatusNotifierItem tray and a freedesktop notification server.
  Developed and tested on KDE Plasma 6.
- Your phone paired and connected with the hands-free profile.

Known shortcoming: no cellular reception indicator. PipeWire receives the value from the
phone but does not publish it on D-Bus, so the tray shows battery only. See `SCOPE.md`.

## Install

Flatpak on Flathub is the intended distribution channel; the first submission is
tracked as build step 11 in `SCOPE.md`. Until then, run from source or build the Flatpak
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
`pipewire-libs` (Fedora) and `pipewire-audio` (Arch). `bluez-obexd` / `bluez-obex`
provides the contacts and messages features; the bus starts it on demand.

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
Ctrl+C in the terminal. Only one instance runs at a time: launching the command again
raises the dialpad of the running one, and `bt-handsfree-kde tel:+15550100` opens it
with the number filled in.

Optional: install the desktop file and icon for your user so the notification server
and the desktop portal can associate the running app with its entry (silences the
"Could not register app ID" warning on start), and make the app the handler for
`tel:` links:

```bash
install -Dm644 data/io.github.MarioStoilov.BtHandsfreeKDE.desktop \
        -t ~/.local/share/applications
install -Dm644 src/bt_handsfree_kde/resources/icon.svg \
        ~/.local/share/icons/hicolor/scalable/apps/io.github.MarioStoilov.BtHandsfreeKDE.svg
update-desktop-database ~/.local/share/applications
xdg-mime default io.github.MarioStoilov.BtHandsfreeKDE.desktop x-scheme-handler/tel
```

The desktop file runs `bt-handsfree-kde`, so for `tel:` links to reach a checkout that
command has to be on your `PATH`, for example by symlinking `.venv/bin/bt-handsfree-kde`
into `~/.local/bin`. The Flatpak needs none of this: its desktop file is exported on
install, and the handler choice is made in System Settings > Applications > Default
Applications, or with the same `xdg-mime` command.

### Contacts

The first time the app reads the contacts, the phone asks whether to allow contact
sharing with this computer (Android) or to sync contacts (iOS). Allow it; the phone
remembers the choice for this computer. Until then the Contacts tab shows the phone's
refusal and a Refresh button.

The contacts stay in memory and are read from the phone again each time it connects.
obexd, which does the transfer, writes the phone's vCard file into the app's cache
directory (`~/.cache/io.github.MarioStoilov.BtHandsfreeKDE`, or the app's own cache
directory in the Flatpak); the file is deleted as soon as it has been parsed. Only
names and telephone numbers are requested, never photos or addresses.

### Messages

Messages use the same obexd service. The phone asks once whether to allow message
access for this computer (Android); on an iPhone, enable "Show Notifications" for the
computer in its Bluetooth settings, which is the only way iOS exposes messages, and
only for SMS and iMessage text. The app reads the 25 newest messages of the inbox and
the sent folder when the phone connects, keeps a connection open so new messages are
pushed as they arrive, and shows each new one as a notification. Opening a conversation
marks its messages as read on the phone. Nothing is stored on disk beyond the transfer
file of a fetched message, which is deleted after parsing.

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
| `--own-name=io.github.MarioStoilov.BtHandsfreeKDE` | Single instance: a second launch (a `tel:` link) hands its number to the running app |
| `--talk-name=org.bluez.obex` | Contacts and messages through obexd |
| `--talk-name=org.kde.StatusNotifierWatcher` | Tray icon |
| `--talk-name=org.freedesktop.Notifications` | Call notifications with buttons |
| `--system-talk-name=org.bluez` | Phone name, connection state, battery |
| `--socket=wayland`, `--socket=fallback-x11`, `--share=ipc` | Windows |
| `--socket=pulseaudio` | Dialpad key tones (call audio itself never passes through the app) |

No network or filesystem access is requested; the transfer files obexd writes land in
the app's own cache directory, which every Flatpak may write.

## Development

```bash
uv run ruff format && uv run ruff check
uv run pytest                            # unit, offscreen widget and D-Bus integration tests
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest flatpak/io.github.MarioStoilov.BtHandsfreeKDE.yml
busctl --user call org.pipewire.Telephony /org/pipewire/Telephony org.freedesktop.DBus.ObjectManager GetManagedObjects
busctl --user call io.github.MarioStoilov.BtHandsfreeKDE /io/github/MarioStoilov/BtHandsfreeKDE \
       io.github.MarioStoilov.BtHandsfreeKDE ShowDialpad s "+15550100"   # what a tel: launch does
```

The integration tests start a private `dbus-daemon` (from the `dbus` package every
desktop already has) and export fake PipeWire, BlueZ, obexd and notification services on
it, so they need neither a phone nor the real services. The Flatpak build runs the same
suite inside the build sandbox.

Coding standards and repository rules are in `CLAUDE.md`.

## License

[MIT](LICENSE). Use it, fork it, ship it — just keep the copyright notice
(which points back to this repo) with your copies.
