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
  just raises the dialpad. Flatpak lets the app own its own ID by default, so the
  manifest needs no `--own-name` entry for it.

### 2. Contacts sync

- The Contacts tab of the main window: a search field (matches name text, or the digits
  typed against the numbers), one row per contact with the numbers in its tooltip, a
  Call button, a Refresh button and a status line. Call dials the contact's only
  number; a contact with several numbers gets a small menu under the button listing
  them by kind (Mobile, Home, Work, ...), preferred number first. Double-click or Enter
  on a row does the same as Call. Once messaging exists, a Message button joins them.
- When it syncs: automatically each time a phone connects over HFP, two seconds after
  the gateway appears so the phone is not asked for a second channel while it sets up
  the first, and on Refresh. A sync already running for that phone is not restarted.
  Pulls and message listings of all phones share one lock on the obexd layer, so
  with several phones they reach obexd one at a time (decision 2026-09-26).
- What is kept: the phonebook lives in memory for the session only, per phone, and is
  dropped when the phone disconnects. Nothing is persisted (decision 2026-09-20).
- Backend: obexd's Phonebook Access client (`org.bluez.obex`, session bus):
  `Client1.CreateSession(address, {Target: "pbap"})`, `PhonebookAccess1.Select("int",
  "pb")`, `PullAll(targetfile, {Format: "vcard30", Fields: [N, FN, TEL]})`, then
  `RemoveSession`. The transfer's end arrives as `PropertiesChanged` on
  `org.bluez.obex.Transfer1` (`Status` `complete` or `error`), after which obexd removes
  the transfer object; the completion signal can arrive before the `PullAll` reply is
  processed, which the client accounts for. Only names and numbers are requested, so
  photos never cross. The phone prompts once to allow contact sharing (Android) or
  "Sync Contacts" (iOS); a refusal surfaces as a failed sync with instructions.
- obexd binds a client session to the D-Bus connection that created it and drops the
  session when that connection closes, so the pull runs on the app's long-lived bus
  connection. (A `busctl call` cannot exercise this API for that reason.)
- obexd writes the vCard file on the host, so the target path is the app's own cache
  directory (`$XDG_CACHE_HOME/<app id>/`, `~/.cache` when unset), whose absolute path is
  identical inside and outside the sandbox. The file gets a random name, mode 0700
  directory, and is deleted right after parsing, also on failure.
- Parsing (`contacts/vcard.py`) reads vCard 2.1 and 3.0: folded lines, quoted-printable soft
  breaks and charsets, 3.0 escapes, bare 2.1 type parameters. Name is `FN`, else
  assembled from `N`, else the first number. Cards without a dialable number are
  dropped. Observed from an Android phone: vCard 3.0 honoured, the field filter
  honoured, numbers delivered as bare digits with or without `+`, types `CELL`,
  `VOICE`, `HOME` with `PREF`.
- Caller ID: HFP delivers numbers only on most phones, so a call whose `Name` is empty
  gets its name from the phonebook of the gateway carrying it (PipeWire never fills
  `Name`; verified in its 1.6.2 source on 2026-10-26). Numbers match by digits; a
  national form matches an international one when one ends with the other, or with
  the other minus its leading national trunk `0`, and what is compared has at least
  seven digits. The trunk rule was added on 2026-10-26 (build step 14): the phone
  reports callers and message senders in international form (`+359…`), while a
  contact may be stored nationally (`0888…`), whose digits are not a suffix of the
  international ones; on the development phone about half of the phonebook was
  stored that way and never matched. The lookup is applied wherever a call reaches a
  view (notification, call window, tray, dialpad status), and views are redrawn when a
  phonebook arrives during a call.
- Sandbox: `--talk-name=org.bluez.obex`.

### 3. Messages

- The Messages tab of the main window: conversations on the left (name, preview of the
  latest message, bold while it holds unread messages), the selected conversation on
  the right as bubbles (incoming left, outgoing right, time under each), a Refresh
  button and a status line. Conversations are grouped by the other party's address
  because the phone tested returns one `ConversationId` for everything; a number in
  national and international form is one conversation (same rule as caller ID).
  Alphanumeric senders (service SMS) are conversations too. Names come from the
  phonebook, else from the name the phone attached, else the address.
- What is loaded: the 25 newest messages of the inbox and of the sent folder, when the
  phone connects (five seconds after the gateway appears, after the phonebook pull) and
  on Refresh (decision 2026-09-20). In memory only, dropped on disconnect. Opening the
  session and listing hold the obexd sync lock shared with the phonebook pulls; the
  open session does not.
- Opening a conversation marks its unread messages as read on the phone
  (`Message1.Read = true`, decision 2026-09-20) and fetches the full text of messages
  whose listing preview may be cut (the listing's `Subject` is the SMS text up to 255
  characters).
- Sending is a later addition (`MessageAccess1.PushMessage` exists in obexd).
- Backend: obexd's Message Access client: `CreateSession(address, {Target: "map"})`,
  then `SetFolder("telecom")`, `SetFolder("msg")` (one level per request),
  `ListMessages("inbox" | "sent", {MaxCount: 25})`, whose reply is a dictionary
  `{message object path: properties}`; `Message1.Get(targetfile, false)` for a full
  message, delivered as a bMessage file (text between `BEGIN:MSG` and `END:MSG`,
  originator vCard before the envelope) into the app's transfer directory, deleted after
  parsing. The MAP session stays open while the phone is connected: creating it makes
  obexd register for the phone's notifications (the phone opens a server session back to
  obexd), and a pushed message surfaces as a `Message1` object appearing below the
  session, which the client completes with a `Get` and announces. A `Message1` object
  disappearing means the phone deleted the message; the session disappearing means the
  link was lost, shown with a Refresh hint.
- Observed from the Android phone tested: `SupportedTypes` SMS_GSM, SMS_CDMA, MMS;
  folders inbox, outbox, sent, deleted, draft; no `Direction` property from obexd 5.85,
  so the folder tells the direction; timestamps `YYYYMMDDTHHMMSS`.
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
- **New message**: normal urgency, category `im.received`, sender name (via contacts)
  or address as the title and the text as the body, cut at 200 characters; an Open
  button shows the Messages tab on that conversation. Shown for every pushed incoming
  message, service SMS included (decision 2026-09-20). Opening the conversation closes
  the notifications of the messages it marks read.
- **Phone connected / disconnected** and **telephony service unavailable**: low urgency,
  informational.
- **Several phones**: while more than one phone is connected, the incoming-call and
  new-message titles carry the phone's name after a separator ("Incoming call · Pixel",
  "Alice Doe · Pixel"); with one phone the titles stay short. A second phone
  connecting while a call is up redraws the tray and the main window only; the call
  window keeps its call, its typed tones and its dialpad state. The `tel:` hand-off
  and the Call buttons act on the phone picked in the main window's chooser.
- Nothing beyond what Bluetooth carries: Android has no profile for mirroring app
  notifications, and iOS's ANCS would be a separate GATT client project. Out of scope.

### 5. Answering and rejecting calls

- Backend: `org.pipewire.Telephony.Call1.Answer` / `Hangup`, plus `HangupAll`,
  `HoldAndAnswer`, `ReleaseAndAnswer`, `ReleaseAndSwap`, `SwapCalls` and
  `CreateMultiparty` on the gateway for waiting and held calls.
- Call state changes arrive through `PropertiesChanged`; call creation and removal through
  `InterfacesAdded` / `InterfacesRemoved` on `/org/pipewire/Telephony`.

### 6. Disconnect and reconnect

What happens when a link or a service goes away, defined and verified on 2026-09-26.
The app never has to be restarted; whatever comes back is picked up from the bus.

- **The phone disconnects** (gateway withdrawn by WirePlumber): its calls are dropped,
  which closes the call window and the incoming-call notification; its phonebook and
  messages are dropped, its MAP session removed, and the new-message notifications
  that would open its conversations are closed; a "Phone disconnected" notification is
  shown. Should the service withdraw the gateway before its calls, the client drops
  the calls itself so none outlives its phone. **Reconnecting** is a new gateway: a
  "Phone connected" notification and the same phonebook pull and message listing as
  on the first connect, with the same delays.
- **WirePlumber restarts** (`org.pipewire.Telephony` leaves and returns): the loss
  clears every gateway and call, shows "Telephony service unavailable" and one
  "Phone disconnected" per phone, and drops the phones' data as above. The return
  re-reads the object tree and announces its calls as removed, changed or added
  against what was known, so a gateway that is already back triggers the syncs, and
  one that reconnects later does the same when it appears. Observed on the host: the
  phone's HFP link drops with WirePlumber and comes back by itself about a second
  later; the phonebook and messages were synced again within ten seconds.
- **obexd restarts or drops the MAP session**: obexd emits no removal signals when it
  dies, so the app follows its bus name and treats every session it held as removed
  when the name goes; a session removed while obexd lives arrives as
  `InterfacesRemoved`. Either way the messages already listed are kept, the state says
  "The messages connection to the phone was lost. Refresh to reconnect.", a transfer
  in flight ends as failed at once instead of waiting for its timeout, and the next
  connect or Refresh opens a new session. A phonebook pull cut the same way reports a
  lost connection too. Observed on the host with `systemctl --user restart obex`.
- **BlueZ restarts** (`org.bluez` leaves and returns on the system bus): the loss
  reports every device as disconnected without battery, so names and battery leave
  the tray; the return re-reads the device list and they come back. BlueZ absent at
  startup is handled the same way: the devices load when the name appears. Observed
  on the host with `sudo systemctl restart bluetooth`: the gateway went first, the
  name left and returned within a tenth of a second with an empty device list (BlueZ
  loads devices after claiming its name, so they arrived as `InterfacesAdded`), the
  phone reconnected a few seconds later, both syncs ran, and the tray tooltip showed
  the phone's name and battery again.
- Each case is covered by an integration test against the fakes (`tests/integration/`,
  `test_application.py` for the wiring). On the host, the obexd, WirePlumber and
  BlueZ restarts and a phone reconnect were run (outcomes above); the phone-side
  Bluetooth toggle during a call is listed under Pending verifications.

### 7. Startup requirements check

Defined 2026-09-26. On launch, before the tray registers, the app checks what it
depends on and keeps running with whatever works:

- The items, in the order shown: PipeWire's telephony service (`org.pipewire.Telephony`
  owned or activatable on the session bus; its presence implies PipeWire 1.4 or newer,
  which introduced the service, so no version is read), BlueZ (`org.bluez` owned on the
  system bus), obexd (`org.bluez.obex` owned or activatable), a notification server
  whose `GetCapabilities` includes `actions` (a server without buttons counts as
  missing, since ringing calls then fall back to the window), a StatusNotifierWatcher
  (owned), and libpulse's simple API loading (key tones).
- What the user sees when something is missing: a window (`ui/dependencies_window.py`)
  with one row per item, an icon for met or missing, the item's name, one sentence on
  what was found and, for a missing item, what provides it in the README's words
  (package names `pipewire`, `wireplumber`, `libspa-0.2-bluetooth`, `bluez`,
  `bluez-obexd`, `libpulse0` / `pulseaudio-libs`, or the desktop component); a
  normal-urgency notification "Missing requirements" listing the item names; a
  "Missing: …" line at the end of the tray tooltip; and a "Missing requirements…"
  entry above Quit in the tray menu that reopens the window (decision 2026-09-26, so
  the window can be found again after it was closed).
- The checks are repeated half a second after any of the watched names changes hands
  on its bus, so a service restart is checked once in its final state. An item that
  becomes available only updates the window, the tooltip and the menu; an item that
  goes missing while the app runs is announced like at startup.
- Backend: `dbus/dependencies.py`, using `NameHasOwner`, `ListActivatableNames` and
  `GetCapabilities`, with its own system bus connection for BlueZ. Inside the sandbox
  the D-Bus proxy answers these for the names the manifest allows, which are exactly
  the ones checked.
- Observed on the host (2026-09-26): a normal launch logs every requirement as
  available and shows no window; an obexd restart triggers exactly one re-check half a
  second later; a launch with the system bus unreachable reports BlueZ missing, opens
  the window, sends the notification and ends the tooltip with "Missing: Bluetooth
  daemon (BlueZ)" while calls, contacts and messages keep working.

### 8. Settings panel

Defined 2026-10-26 (build step 13). One window, "BT Handsfree settings"
(`ui/settings_window.py`), replaces the earlier separate settings and About windows. It
has three tabs and a Close button; the tray opens it on the tab that matches the entry
clicked.

- **Phones** (`ui/phones_page.py`): two groups. "Connected now" lists every telephony
  gateway: the phone's icon, its BlueZ alias (the address when BlueZ has no entry),
  the Bluetooth address and the battery percentage when BlueZ reports one.
  "Previously connected" lists the phones paired with this computer that have no
  gateway right now, with alias, address and either "Connected, no hands-free link"
  (BlueZ says connected, so the phone reaches the computer but PipeWire has no HFP
  link to it) or "Not connected". Each group has an empty-state line ("No phone
  connected", "No other phone is paired") and, while BlueZ is unreachable, says
  "Bluetooth service not available".
- Source of the history: BlueZ's own pairing list, so the app persists nothing. A
  device counts as a phone when its `org.bluez.Device1` has `Paired` true and
  `UUIDs` contains the Hands-Free Audio Gateway service
  (`0000111f-0000-1000-8000-00805f9b34fb`). Verified on the host (2026-10-26): seven
  device objects, of which three TVs advertise the same UUID but are unpaired, and
  only the phone passes both tests. The `Icon` property (a freedesktop icon name such
  as `phone`) picks the row icon, with `smartphone` as the fallback. BlueZ does not
  put a last-connected time on D-Bus (it stays in its root-only storage), so the list
  is ordered by name and carries no dates. The list follows BlueZ's signals, so
  pairing or removing a phone in the system Bluetooth settings updates it at once.
- **Call audio** (`ui/call_audio_page.py`): the computer's device choosers defined
  under "Call audio devices", then what the settings window held before: one group
  per connected phone with a speaker slider and a microphone slider over the HFP
  gain range (0–15, shown as %), the "Play call audio on this computer" checkbox
  and the negotiated codec while an audio link is open.
- **About** (`ui/about_page.py`): the About content unchanged: application icon,
  name, version from `__version__`, the one-line description, links to the
  repository, the issue tracker and the MIT license, the copyright line and the
  vibe-coded disclosure from the README.
- Tray: a "Settings…" entry below Messages replaces "About BT Handsfree" and opens
  the Phones tab; the "Speaker N %" and "Microphone N %" entries open the Call audio
  tab.
- Backend: `dbus/bluez.py` reads `Paired`, `Icon` and `UUIDs` next to the properties
  it already tracked and offers the paired phones as a list; nothing new is read from
  any other service, and the Flatpak manifest is unchanged (`org.bluez` was already
  allowed on the system bus).

### 9. Call audio devices

Defined 2026-10-26 (build step 15). The Call audio tab lets the user choose which of the
computer's speakers and microphones carry a call, instead of whatever the system
default devices are at the time.

- **How call audio flows.** While an audio link to the phone is open, PipeWire's
  Bluetooth device exposes two nodes for it: a source carrying the far end's voice
  (`bluez_input.<address>.N`, class `Audio/Source`) and a sink that takes what the
  phone should hear (`bluez_output.<address>.N`, class `Audio/Sink`), both with
  `api.bluez5.profile = headset-audio-gateway` and `node.autoconnect = true`.
  WirePlumber links such nodes through its ordinary target hooks: a `target.object`
  entry for the node in the `default` metadata wins, otherwise the default sink or
  source. The app therefore sets `target.object` on the phone's source node to the
  chosen speaker's `node.name`, and on the phone's sink node to the chosen
  microphone's `node.name`; WirePlumber re-links as soon as the entry changes
  (`linking/rescan` reacts to `target.object` changes) and, since these nodes carry
  no routes, also stores the target itself under `node.stream.restore-target`.
  Verified in WirePlumber 0.5.13's scripts and PipeWire 1.6.2's sources on
  2026-10-26. Clearing the entry returns the node to the system default.
- **The first non-D-Bus client.** Metadata lives on PipeWire's native protocol, so
  `audio/pipewire.py` talks to libpipewire over ctypes, as `dtmf.py` does with
  libpulse: a thread loop, a context connected with `media.category = Manager`
  (which is what WirePlumber requires to grant a sandboxed client write access),
  the registry for the node list and the `default` metadata object for writing.
  The registry's `global` event carries only a fixed subset of a node's properties
  (name, description, media class and a few more), without the `api.bluez5.*` keys
  that mark a phone's nodes, so every node is bound and its full property set is
  taken from its `info` event, as `pw-dump` does. Callbacks run on PipeWire's
  thread and are handed to the Qt thread. The app still never carries audio.
- **What the tab shows.** A "This computer" group with "Play calls on" and
  "Microphone" choosers. Each lists "System default" and every current sink or
  source by its `node.description` (the phone's own nodes excluded), and shows a
  saved device that is not present right now as "<name> (not available)" so the
  choice is visible while a headset is off. Below the group a line says when
  PipeWire cannot be reached ("PipeWire not reachable; calls use the system
  default devices"); the phone groups with the sliders follow as before.
- **Persistence.** The choice is the first thing the app stores: `node.name` of the
  speaker and of the microphone, in `settings.ini` under the app's XDG config
  directory (`audio/preferences.py`, the single settings module; an empty value
  means the system default). Device names are not personal data. The choice is
  applied to every phone's nodes each time they appear, so it also holds when
  WirePlumber's own restore is disabled, and it is applied at once to nodes of a
  call in progress.
- **Sandbox**: `--filesystem=xdg-run/pipewire-0` for the native socket; the KDE
  runtime ships `libpipewire-0.3.so.0`. The KDE SDK ships no PipeWire daemon, so the
  native client has no private-daemon test; the device list derivation, the target
  choice per node, the preferences file and the router logic are unit-tested and
  the client itself is verified live.

## Tray icon and menu

- The icon reflects call state (idle, incoming, active). The tooltip shows the phone's
  name and battery percentage (`org.bluez.Battery1` on the phone's device object).
- The app icon is delivered as pixels, not only by name: the tray item always carries
  `IconPixmap` next to `IconName`, and every notification carries the `image-data`
  hint next to the icon reference. Inside a Flatpak the host resolves icon names with
  its own search path, which only includes Flatpak's export directory after a login
  that followed the Flatpak installation; a name alone left the tray item blank and
  notifications with a generic icon (observed with the 0.1.1 bundle, 2026-10-23).
- Left and right click both open the same dropdown menu. Qt's tray class cannot do
  this on Plasma (it hardcodes `ItemIsMenu = false` and opens its own popup, which
  Wayland refuses), so the app implements the `org.kde.StatusNotifierItem` and
  `com.canonical.dbusmenu` protocols itself (`dbus/statusnotifier.py`,
  `dbus/dbusmenu.py`). Plasma
  renders the menu natively; entries can carry theme icons but no custom widgets, so
  each entry opens the matching window:
  - one section per connected phone: its name and battery as a header, then the active
    or incoming call (caller, state) with Answer / Reject / Hold / Hang up, then
    "Speaker N %" and "Microphone N %" with speaker and microphone icons (clicking
    either opens the settings window) and the "Call audio on this computer" checkmark
    toggle (`AudioGatewayTransport1.RejectSCO` / `Activate`)
  - after the phone sections, the tabs of the main window: Dialpad, Contacts,
    Messages (with the unread count in its label when there is one), then "Settings…"
  - Quit
- The Dialpad, Messages and Contacts windows are tabs of one window (`ui/main_window.py`);
  a menu entry opens it on that tab, and the `tel:` hand-off opens the Dialpad tab. A
  phone chooser sits above the tabs and is shown only when several phones are
  connected; every tab acts on the chosen phone.
- **Settings window**: the Phones, Call audio and About tabs described under
  "Settings panel" in Features. The URLs on the About tab live once in
  `bt_handsfree_kde/__init__.py` and are mirrored in pyproject.toml and the MetaInfo
  file.

## Known shortcomings

- **No reception (cellular signal) indicator.** The phone sends signal strength over HFP
  (`+CIEV` indicator `signal`, 0–5) and PipeWire's native backend parses it, but only
  writes it to its debug log; it is not a property on
  `org.pipewire.Telephony.AudioGateway1`, and no other component on Linux carries a
  phone's cellular signal. The tray therefore shows battery only. Adding a
  `SignalStrength` property upstream in PipeWire would close this; until such a version
  exists, nothing in the UI refers to reception.
- **No charging indicator for the phone's battery.** Over HFP the phone (the audio
  gateway) reports its battery only through the `battchg` indicator, an integer 0–5
  with no charging flag; the HFP specification defines no other battery indicator in
  that direction. PipeWire's native backend scales it to a percentage in steps of
  20 and hands it to BlueZ's battery provider, whose `org.bluez.Battery1` carries
  `Percentage` and `Source` only. UPower mirrors that object with an unknown state.
  The Apple (`+IPHONEACCEV`) and Plantronics (`+XEVENT`) extensions that PipeWire
  parses carry a headset's battery towards a phone, the opposite direction, so they
  cannot help. The UI therefore shows the percentage alone and never a charging
  state. Verified on 2026-10-26 against PipeWire 1.6.2 `backend-native.c`, BlueZ
  5.85 and UPower on the development host.
- **iOS messaging is limited** as described under Messages.
- **Call audio codec** is whatever PipeWire negotiated (CVSD or mSBC); the app displays it
  but cannot change it.

## Requirements

- PipeWire 1.4 or newer with the native HFP backend (not oFono) and its telephony D-Bus
  service enabled (default). Verified against PipeWire 1.6.2 / WirePlumber 0.5.13.
- BlueZ with obexd (`bluez-obexd` on Debian/Ubuntu) for contacts and messages.
- A desktop with StatusNotifierItem and a freedesktop notification server; developed on
  KDE Plasma 6.
- Tested against one Android phone (Samsung, One UI) only. iOS is untested: same D-Bus
  surface, but caller-name delivery, the permission prompts for contacts and messages
  and the MAP limits under Messages are expected to differ.

## Build order

Each step ends runnable against a real phone; the observed outcome is stated when the step
is reported done.

1. Call control (answer, reject, hang up, hold), incoming and active-call notifications,
   tray icon with phone name, battery and the call entry in the menu.
2. Dialpad window with DTMF, `tel:` handler, single instance over D-Bus.
3. Contacts sync window; caller-ID name lookup on incoming calls and in notifications.
4. Messages tab; new-message notifications.
5. **About window.** A tray entry "About BT Handsfree" below Messages opens a window
   with the application icon, name and version, the one-line description, links to
   the repository, the issue tracker and the MIT license text, the vibe-coded
   disclosure from the README, and a Close button, in the shape of simpleStonks' about
   dialog. Version comes from `__version__`. Since step 13 this content is the About
   tab of the settings window.
6. **Platform note.** README and the MetaInfo description state that every feature was
   developed and tested against one Android phone (Samsung, One UI) and that iOS is
   untested: the D-Bus surface is the same, but caller-name delivery, the contact and
   message permission prompts and the MAP limits described under Messages are
   expected to differ. Done when the wording is in both files.
7. **Automated tests.** pytest with three layers. Unit tests for the pure modules
   (`phone_numbers`, `contacts/vcard`, `contacts/phonebook`, `messages/bmessage`,
   `messages/message`, `messages/conversations`, `dbusmenu` shape and numbering).
   Widget tests under the offscreen Qt platform for the pages, the main window, the call
   window and the tray's menu tree. Integration tests that start a private
   `dbus-daemon`, export fake `org.pipewire.Telephony`, `org.bluez.obex` and
   `org.freedesktop.Notifications` services with dbus-fast and drive the real clients
   through call lifecycles, a PBAP pull, a MAP listing and a pushed message, and the
   single-instance hand-off. The convention in CLAUDE.md that defers tests until the
   client layer is stable is replaced by "every change comes with its tests" in the
   same step. Done when `uv run pytest` is green and runs in the Flatpak build too.
8. **Disconnect and reconnect.** Define and verify what happens when: the phone
   disconnects and reconnects (gateway removed and added: phonebook and messages are
   dropped and synced again, a call in progress is closed everywhere, notifications
   are withdrawn); WirePlumber restarts (telephony name lost and regained: the client
   re-synchronises, gateways reappear and trigger the same syncs); obexd restarts or
   drops the MAP session (the messages state says the connection was lost and the next
   connect or Refresh reopens it); BlueZ restarts (phone names and battery return on
   their own). Each case is exercised on the host by toggling Bluetooth on the phone
   and restarting the services, and the observed outcome is stated. Defined under
   "Disconnect and reconnect" in Features.
9. **Several phones.** The tray already has one section per phone and the main window
   a chooser; the rest is defined here: notifications name the phone when more than
   one is connected; the `tel:` hand-off dials from the phone selected in the chooser;
   per-phone syncs run one at a time so two phones do not compete for obexd; a second
   phone connecting while a call is up does not disturb the call window. Verified
   with the fake telephony service from step 7 exposing two gateways, and on the host
   when a second phone is available. Defined under Notifications, "Several phones",
   and in the sync bullets of Contacts and Messages (2026-09-26); the host run with a
   second phone is listed under Pending verifications.
10. **Startup dependency check.** On launch, before the tray appears, the app checks
    what it depends on and tells the user what is missing or failed, then keeps
    running with the features that work: PipeWire's telephony name on the session bus
    (owned or activatable, and PipeWire 1.4 or newer), BlueZ on the system bus, obexd
    activatable on the session bus, a notification server with actions, a
    StatusNotifierWatcher, and libpulse-simple for key tones. The result is shown as a
    notification plus a window listing each missing item with the distro package or
    setting that provides it (the same names as in the README), and in the tray
    tooltip while it lasts. Checks are repeated when a name appears or leaves the bus.
    Defined under "Startup requirements check" in Features (2026-09-26).
11. **Release bundles.** Each release is a git tag `v<version>` on a commit whose
    MetaInfo release entry and `__version__` carry that version. Pushing the tag runs
    `.github/workflows/release.yml`: inside the Flathub-maintained KDE 6.10 container
    image it builds the tagged checkout with the repository's own manifest (test suite
    included), exports a single-file bundle for x86_64 with Flathub as the runtime
    repository, and attaches it to the GitHub release of the tag. The build passes
    `--default-branch=stable` (the manifest cannot carry the branch; Flathub's linter
    rejects the key), so a newer bundle upgrades an existing installation. Users
    install the bundle with `flatpak install`; there is no automatic update.
    Decided 2026-10-23 after the Flathub route was tried and dropped: Flathub's
    generative-AI policy forbids AI-assisted manifests and AI-run submission pull
    requests, which this project cannot honestly satisfy, and its rules on tray-only
    and short-lived projects made acceptance unlikely. The Snap Store was ruled out
    because strict confinement cannot reach `org.pipewire.Telephony` or obexd on the
    session bus and classic confinement is not granted for session-bus access.
    The store-facing assets stay: the screenshot `data/screenshots/dialpad.png` (the
    Dialpad tab with the fictional example number; no tab that shows personal data
    is ever captured), referenced from the MetaInfo by its raw GitHub URL at the
    release tag, branding colours, lowercase AppStream developer id, and the Network
    and Telephony categories. Manifest facts that still apply: the KDE SDK ships
    libdbus but no `dbus-daemon`, so the manifest builds dbus as a build-time-only
    module (cleaned up afterwards) for the integration tests; the install step
    compiles the package afresh and the test run writes no bytecode, so nothing from
    the test run stays in the installed app; owning the app's own bus name needs no
    `--own-name` entry. Verified on the host with flatpak-builder 1.4.8 on
    2026-10-23: the sandbox build runs all 101 tests, the sandboxed app registers
    its tray item, syncs the phonebook through obexd into its own cache directory
    (the transfer file is gone after parsing), a second sandboxed launch hands its
    `tel:` number over and exits, the exported desktop file registers the `tel:`
    scheme, and a locally exported bundle installs and runs. Done when the workflow
    has produced the bundle for a pushed tag and that bundle installs and runs from
    the release page.

12. **Charging indicator investigation.** Find out whether the phone's charging state
    can be shown next to its battery percentage. Outcome (2026-10-26): it cannot; the
    reasons are recorded under Known shortcomings, "No charging indicator", and the
    UI omits the item. Done.
13. **Settings panel.** One window that houses what was spread over the settings
    and About windows: the per-phone speaker and microphone sliders, audio routing
    and codec; the About content; the phones currently connected; and the phones
    that were connected before, taken from BlueZ's pairing list so the app stores
    nothing (decided 2026-10-26 over an app-kept history). Defined under "Settings
    panel" in Features. Observed on the host (2026-10-26): with the phone linked,
    the Phones tab lists it under "Connected now" with address and battery and says
    "No other phone is paired" below; disconnecting the phone from the computer
    moves it to "Previously connected · Not connected" within a second, and
    reconnecting moves it back with the battery restored; the Call audio tab shows
    the sliders and the routing checkbox; "Settings…" and the volume entries open
    the matching tab. The sandbox needs no new permission, so the Flatpak was not
    rebuilt for this step. Done.
14. **Contact names in the call window.** On an outgoing or incoming call whose number
    is in the synced phonebook, the call window shows the contact's name instead of
    the bare number. Cause (2026-10-26): the number-matching rule required the
    national form to be a digit suffix of the international one, which fails for
    every contact stored with a national trunk `0`; the rule now sets that `0`
    aside. Defined under Contacts sync, "Caller ID". Verified on the host against
    the live phonebook and message list without placing a call (2026-10-26): the
    phonebook held 104 international and 83 national numbers, and an international
    message sender that matched no contact before resolves to one now; the lookup is
    shared by calls, notifications and conversations. The call window itself was
    not exercised with a live call; that stays under Pending verifications.
15. **Audio device choice.** Next to the volume and microphone settings, a choice of
    which of the computer's speakers and microphones carry the call audio. Defined
    under "Call audio devices" in Features (2026-10-26). Observed on the host
    (2026-10-26): the Call audio tab lists the computer's five devices by
    description, a stored speaker is selected by its description and a stored
    microphone that is absent shows as "(not available)"; with a throwaway null
    node carrying a phone's profile and address, the app wrote `target.object` with
    the stored microphone for the sink-like node and the system default for the
    source-like node, rewrote both when the choice changed, and `pw-metadata`
    showed the entries; the sandboxed app connects with `media.category = Manager`
    and `pipewire.sec.flatpak = true` and writes the same entries. A real call
    with a chosen device is listed under Pending verifications.

Later, unscheduled: sending messages, reception once PipeWire exposes it.

## Backlog

Work that is known and not scheduled to a build step. An item leaves this list when it
is done and the observed outcome has been stated.

### Pending verifications

Behaviour that is implemented but has not yet been exercised against a real phone or
the sandbox. Each is checked at the next opportunity that provides what it needs.

- **Call audio on a chosen device** (step 15): with a speaker or microphone other
  than the system default chosen in the Call audio tab, a call's far-end voice plays
  on that speaker and the phone receives that microphone, as the metadata written for
  the phone's nodes requests. Exercised with throwaway nodes carrying a phone's
  properties only; the development host had no call during the step.
- **Caller ID on a live incoming call** (steps 3 and 14): a call from a number in
  the synced phonebook, arriving with an empty HFP `Name`, shows the contact's name
  in the notification, the call window and the tray, including a contact stored in
  national form (`0…`) when the phone reports the international number. Needs a
  caller who is in the phone's contacts, or an outgoing call to one.
- **Contacts tab actions on the live phonebook** (step 3): Refresh re-pulls and updates
  the status line; Call on a contact with several numbers offers the number menu and
  dials the chosen one. Exercised offscreen with synthetic vCards only.
- **Messages tab actions on the live phone** (step 4): marking read on open clears
  the unread state on the phone, and a long message's full text is fetched when its
  conversation is opened. The pushed-message path itself (notification with Open,
  unread count in the tray) was verified with a service SMS on 2026-09-20.
- **Local DTMF key tones during a call** (step 1): whether the far end hears the local
  key beep, which could happen if WirePlumber makes the phone's HFP link the default
  sink while a call is up. If it leaks, playback is pinned to a local sink
  (`pa_simple_new` takes a device name) instead of the default.
- **Two phones on the host** (step 9): with a second phone connected, the notification
  titles name the phone, the chooser drives the hand-off and the syncs of the second
  phone follow the first. Exercised with the fake services (two gateways) only; the
  development host has one phone.
- **Phone-side Bluetooth toggle during a call** (step 8): turning Bluetooth off on the
  phone while a call is up closes the call window and the incoming-call notification.
  The gateway removal and the re-syncs on reconnect were exercised without a call;
  the call part is covered by the fake only.
