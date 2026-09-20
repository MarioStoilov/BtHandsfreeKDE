# Product scope

What BT Handsfree does, how each feature maps onto the Linux Bluetooth stack, what is
deliberately out, and the order in which it is built. Decisions here were taken in
conversation on 2026-09-19 and are changed here, not in code comments.

## Features

### 1. Dialpad, with phone-link integration

- A window with a number field, a 12-key pad and a Call button. Each key press plays the
  key's tone locally and inserts the key; the field also takes typed or pasted numbers,
  including formatting (spaces, hyphens, dots, parentheses), which is stripped before
  dialling. When several phones are connected a chooser above the field selects the one
  to call from; with one phone it is hidden. Call is enabled only when a phone is
  connected and the field holds something dialable.
- During an active call on the selected phone the same keys also send DTMF tones to it,
  and a status line says so ("Keys send tones to <caller>"), like a phone's in-call
  dialpad. Pressing Call during a call still dials; what happens to the current call is
  the phone's decision.
- Backend: `Dial(number)` and `SendTones(tones)` on
  `org.pipewire.Telephony.AudioGateway1`.
- The desktop file registers the app as the handler for the `tel:` URI scheme
  (`MimeType=x-scheme-handler/tel`, `Exec=... %u`). The URI is read per RFC 3966: visual
  separators and percent-encoding are handled, parameters after `;` (`phone-context`,
  `ext`) are ignored, and a `tel://` authority marker is tolerated. The dialpad opens
  prefilled; the call is not placed until the user presses Call.
- Single instance: the first process owns the app's D-Bus name
  (`io.github.MarioStoilov.BtHandsfreeKDE`, session bus) and serves
  `/io/github/MarioStoilov/BtHandsfreeKDE` with one method, `ShowDialpad(s number)`. A
  later launch hands its number over that method and exits. A launch without a `tel:`
  argument (the app menu entry while the app is running) sends an empty number, which
  just raises the dialpad. The Flatpak owns that name (`--own-name`).

### 2. Contacts sync

- A window with a searchable contact list; each entry has a Call button and, once
  messaging exists, a Message button.
- Backend: obexd's Phonebook Access client (`org.bluez.obex`, session bus):
  `Client1.CreateSession(address, {Target: "pbap"})`, `PhonebookAccess1.Select("int",
  "pb")`, `PullAll(targetfile, filters)`; `Search`/`Pull` for single lookups. The phone
  prompts once to allow contact sharing (Android) or "Sync Contacts" (iOS).
- obexd writes the vCard file on the host, so the target path is always the app's own
  cache directory, whose absolute path is identical inside and outside the sandbox.
- The synced phonebook is also the caller-ID source: HFP delivers numbers only, so names on
  incoming calls and in the call notification come from a number lookup in the local copy.
- Sandbox: `--talk-name=org.bluez.obex`.

### 3. Messages

- A window listing conversations and a thread view. Sending is a later addition
  (`MessageAccess1.PushMessage` exists in obexd).
- Backend: obexd's Message Access client: `CreateSession(address, {Target: "map"})`,
  `MessageAccess1.SetFolder`, `ListFolders`, `ListMessages`, `UpdateInbox`;
  `Message1.Get(targetfile, attachment)` and its properties (`Sender`, `SenderAddress`,
  `Timestamp`, `Subject`, `Read`, ...). obexd runs a built-in Message Notification Service
  server, so the phone pushes `NewMessage`, `MessageDeleted`, `DeliverySuccess` and
  `SendingSuccess` events, which surface as message objects appearing on the bus.
- Platform reality: Android exposes MAP fully. iOS exposes it only when "Show
  Notifications" is enabled for the paired computer in its Bluetooth settings, and only
  for SMS/iMessage text. iOS is best-effort for this feature.
- Sandbox: `--talk-name=org.bluez.obex` (shared with contacts).

### 4. Notifications

Desktop notifications, through `org.freedesktop.Notifications`, for every event the
phone delivers over Bluetooth:

- **Incoming call**: critical urgency, does not expire, actions Answer and Reject.
- **Active call**: shown in the call window, not in a notification, because
  notification buttons cannot carry icons or a dialpad. The window is small and always
  on top: caller name or number, running duration, a Hold/Resume icon button (pause /
  play), a red Hang up icon button (`call-stop`), and a Dialpad toggle that expands a
  12-key DTMF pad, collapsed by default. Like a phone, each key press plays its
  dual-tone locally (synthesised in `dtmf.py`, played through libpulse's simple API,
  which PipeWire serves) and appends the key to a display above the pad; the tone the
  far end hears is sent by the phone via `SendTones`. Hold maps to `AudioGateway1.SwapCalls`, which
  places the lone active call on hold and resumes it on the next press. When the
  notification server lacks actions or persistence (`GetCapabilities`), ringing calls
  are shown in the same window with Answer and Reject instead of a notification.
- **New message**: normal urgency, sender name (via contacts) or address and a text
  preview; an action opens the Messages window on that thread.
- **Phone connected / disconnected** and **telephony service unavailable**: low urgency,
  informational.
- Nothing beyond what Bluetooth carries: Android has no profile for mirroring app
  notifications, and iOS's ANCS would be a separate GATT client project. Out of scope.

### 5. Answering and rejecting calls

- Backend: `org.pipewire.Telephony.Call1.Answer` / `Hangup`, plus `HangupAll`,
  `HoldAndAnswer`, `ReleaseAndAnswer`, `ReleaseAndSwap`, `SwapCalls` and
  `CreateMultiparty` on the gateway for waiting and held calls.
- Call state changes arrive through `PropertiesChanged`; call creation and removal through
  `InterfacesAdded` / `InterfacesRemoved` on `/org/pipewire/Telephony`.

## Tray icon and menu

- The icon reflects call state (idle, incoming, active). The tooltip shows the phone's
  name and battery percentage (`org.bluez.Battery1` on the phone's device object).
- Left and right click both open the same dropdown menu. Qt's tray class cannot do
  this on Plasma (it hardcodes `ItemIsMenu = false` and opens its own popup, which
  Wayland refuses), so the app implements the `org.kde.StatusNotifierItem` and
  `com.canonical.dbusmenu` protocols itself (`statusnotifier.py`, `dbusmenu.py`). Plasma
  renders the menu natively; entries can carry theme icons but no custom widgets, so
  each entry opens the matching window:
  - one section per connected phone: its name and battery as a header, then the active
    or incoming call (caller, state) with Answer / Reject / Hold / Hang up, then
    "Speaker N %" and "Microphone N %" with speaker and microphone icons (clicking
    either opens the settings window) and the "Call audio on this computer" checkmark
    toggle (`AudioGatewayTransport1.RejectSCO` / `Activate`)
  - after the phone sections, the windows that are not tied to one phone: Dialpad,
    Messages, Contacts
  - Quit
- The Dialpad, Messages and Contacts windows are tabs of one window; a menu entry opens
  it on that tab.
- **Settings window**: one group per connected phone with a speaker slider and a
  microphone slider over the HFP gain range (0–15, shown as %), the audio routing
  checkbox and the negotiated codec while an audio link is open.

## Known shortcomings

- **No reception (cellular signal) indicator.** The phone sends signal strength over HFP
  (`+CIEV` indicator `signal`, 0–5) and PipeWire's native backend parses it, but only
  writes it to its debug log; it is not a property on
  `org.pipewire.Telephony.AudioGateway1`, and no other component on Linux carries a
  phone's cellular signal. The tray therefore shows battery only. Adding a
  `SignalStrength` property upstream in PipeWire would close this; until such a version
  exists, nothing in the UI refers to reception.
- **iOS messaging is limited** as described under Messages.
- **Call audio codec** is whatever PipeWire negotiated (CVSD or mSBC); the app displays it
  but cannot change it.

## Requirements

- PipeWire 1.4 or newer with the native HFP backend (not oFono) and its telephony D-Bus
  service enabled (default). Verified against PipeWire 1.6.2 / WirePlumber 0.5.13.
- BlueZ with obexd (`bluez-obexd` on Debian/Ubuntu) for contacts and messages.
- A desktop with StatusNotifierItem and a freedesktop notification server; developed on
  KDE Plasma 6.

## Build order

Each step ends runnable against a real phone; the observed outcome is stated when the step
is reported done.

1. Call control (answer, reject, hang up, hold), incoming and active-call notifications,
   tray icon with phone name, battery and the call entry in the menu.
2. Dialpad window with DTMF, `tel:` handler, single instance over D-Bus.
3. Contacts sync window; caller-ID name lookup on incoming calls and in notifications.
4. Messages window; new-message notifications.
5. Flathub submission: git-pinned manifest, screenshot, first release.

Later, unscheduled: sending messages, reception once PipeWire exposes it.
