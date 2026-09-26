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
  gets its name from the phonebook of the gateway carrying it. Numbers match by digits;
  a national form matches an international one when one ends with the other and the
  shorter has at least seven digits. The lookup is applied wherever a call reaches a
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

## Tray icon and menu

- The icon reflects call state (idle, incoming, active). The tooltip shows the phone's
  name and battery percentage (`org.bluez.Battery1` on the phone's device object).
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
    Messages (with the unread count in its label when there is one)
  - Quit
- The Dialpad, Messages and Contacts windows are tabs of one window (`ui/main_window.py`);
  a menu entry opens it on that tab, and the `tel:` hand-off opens the Dialpad tab. A
  phone chooser sits above the tabs and is shown only when several phones are
  connected; every tab acts on the chosen phone.
- **Settings window**: one group per connected phone with a speaker slider and a
  microphone slider over the HFP gain range (0–15, shown as %), the audio routing
  checkbox and the negotiated codec while an audio link is open.
- **About window** (`ui/about_window.py`, tray entry "About BT Handsfree" below
  Messages): application icon, name, version from `__version__`, the one-line
  description, links to the repository, the issue tracker and the MIT license, the
  copyright line, the vibe-coded disclosure from the README, and a Close button. The
  URLs live once in `bt_handsfree_kde/__init__.py` and are mirrored in pyproject.toml
  and the MetaInfo file.

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
   dialog. Version comes from `__version__`.
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
11. Flathub submission: git-pinned manifest, screenshot, first release.

Later, unscheduled: sending messages, reception once PipeWire exposes it.

## Backlog

Work that is known and not scheduled to a build step. An item leaves this list when it
is done and the observed outcome has been stated.

### Pending verifications

Behaviour that is implemented but has not yet been exercised against a real phone or
the sandbox. Each is checked at the next opportunity that provides what it needs.

- **Caller ID on a live incoming call** (step 3): a call from a number in the synced
  phonebook, arriving with an empty HFP `Name`, shows the contact's name in the
  notification, the call window and the tray. Needs a caller who is in the phone's
  contacts.
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
- **Flatpak sandbox** (steps 2 and 3): the `--own-name` hand-off between two sandboxed
  launches, the exported desktop file's `tel:` registration, and obexd writing the
  phonebook file into the sandboxed cache directory. Needs a machine with
  flatpak-builder; the development host has none.
