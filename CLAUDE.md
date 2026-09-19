# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A KDE Plasma tray application that turns the desktop into a Bluetooth hands-free unit for a
phone (Android or iOS): call control, dialpad, contacts, messages and the notifications
that go with them. The feature set, the mapping of each feature onto the Bluetooth stack,
known shortcomings and the build order live in **`SCOPE.md`**; read it before proposing or
implementing a feature, and change it there when a decision changes.

It is a pure D-Bus client: PipeWire's native HFP backend does the Bluetooth work and
publishes call control on the session bus as `org.pipewire.Telephony`; BlueZ
(`org.bluez`, system bus) provides device name, connection state and battery; obexd
(`org.bluez.obex`) provides contacts and messages; the tray icon is a StatusNotifierItem
and alerts use `org.freedesktop.Notifications`. The app never touches audio itself.

Language and toolkit: Python 3.12+, PySide6 (tray, menus, windows), `dbus-fast` for D-Bus
with `qasync` bridging asyncio onto the Qt event loop. Distribution: Flatpak, published
on Flathub under the app ID `io.github.MarioStoilov.BtHandsfreeKDE`; the code repository
is `github.com/MarioStoilov/BtHandsfreeKDE`.

## Never reason from "this machine"

Any developer must be able to clone and work, and any user with a compatible desktop must
be able to run the Flatpak. Therefore:

- No Bluetooth address, phone model, host name, home directory or D-Bus object path of a
  specific device is hard-coded in code, tests, docs or scripts. Audio gateways and calls
  are discovered at runtime through `org.freedesktop.DBus.ObjectManager` on
  `org.pipewire.Telephony`; the app handles zero, one or several gateways.
- The hard requirement is PipeWire 1.4 or newer with the native HFP backend (not oFono)
  and its telephony D-Bus service enabled. When that service is absent the app says so in
  the tray tooltip and a notification; it never crashes or spins. Contacts and messages
  additionally need obexd; when it is absent those menu entries explain why.
- A known shortcoming listed in `SCOPE.md` (e.g. no reception indicator) is not worked
  around with guesses or placeholders; the UI simply omits the item.
- Behaviour that differs between Android and iOS (caller-name delivery, battery reporting)
  is handled by feature detection on the D-Bus properties, never by guessing the vendor.
- The app is developed and tested both on the host and inside the Flatpak sandbox. A
  D-Bus name the app talks to must be listed in the Flatpak manifest in the same change
  that introduces the call; unfiltered `--socket=session-bus` or `--socket=system-bus` is
  never used.
- `README.md` is the single user-facing source for requirements and installation. If a
  setup step is discovered that a developer or user would need, add it there in the same
  change.
- Personal data (phone numbers, contact names, call history) stays in memory for the
  running session unless a feature explicitly persists it; nothing of the kind is ever
  committed, logged at INFO level or used in examples. Examples use the fictional
  `+15550100` number range.

## Git: never commit or push unless explicitly told to

- Make changes in the working tree and stop. Commit only when the user says "commit";
  push only when the user says "push". A confirmed decision or a finished step is not an
  instruction to commit.
- Commit messages carry no `Co-Authored-By` or other trailer lines. Subject + body only.
- Never rewrite history that has been pushed.

## No placeholder code

Every function, class, module, dependency, test marker or config entry in the tree is
used and fully implemented at the time it is added. No stubs, no "not implemented yet",
no `TODO` scaffolding, no commands that exit with a message about a later step, no
declared-but-unused dependencies. Add a thing in the same change that makes it work.
Roadmap items in this file are documentation, not code.

## Exceptions to any rule in this file

An exception is made only after asking the user and receiving explicit approval for that
specific case. Never assume an earlier exception extends to a new case. Granted exceptions
are listed here:

- (none yet)

## Commands

```bash
uv sync                                         # installs the package + dev tools
uv run ruff format && uv run ruff check
uv run bt-handsfree                             # run from the working tree (host, outside the sandbox)
flatpak-builder --user --install --force-clean build-dir flatpak/io.github.MarioStoilov.BtHandsfreeKDE.yml
flatpak run io.github.MarioStoilov.BtHandsfreeKDE
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest flatpak/io.github.MarioStoilov.BtHandsfreeKDE.yml
busctl --user call org.pipewire.Telephony /org/pipewire/Telephony org.freedesktop.DBus.ObjectManager GetManagedObjects
                                                # inspect the live telephony objects while debugging
```

## Architecture

The D-Bus surface the app consumes (verified against PipeWire 1.6 / WirePlumber 0.5):

- **`org.pipewire.Telephony`** (session bus, owned by WirePlumber). Root object
  `/org/pipewire/Telephony` implements `ObjectManager`; one child per connected phone
  (`.../agN`) with `org.pipewire.Telephony.AudioGateway1` (`Dial`, `HangupAll`,
  `SendTones`, `SwapCalls`, `HoldAndAnswer`, `ReleaseAndAnswer`, `ReleaseAndSwap`,
  `CreateMultiparty`; properties `Address`, `SpeakerVolume`, `MicrophoneVolume`) and
  `org.pipewire.Telephony.AudioGatewayTransport1` (`State`, `Codec`, `RejectSCO`,
  `Activate`). Calls appear as children of the gateway with
  `org.pipewire.Telephony.Call1` (`Answer`, `Hangup`; properties `State`,
  `LineIdentification`, `Name`, `IncomingLine`, `Multiparty`). Call lifecycle arrives via
  `InterfacesAdded` / `InterfacesRemoved`; state changes via `PropertiesChanged`.
- **`org.bluez`** (system bus): `org.bluez.Device1` for `Name`, `Alias`, `Connected`;
  `org.bluez.Battery1.Percentage`. The gateway's `Address` maps to the BlueZ object path
  by the usual `dev_XX_XX_...` convention, resolved through BlueZ's own `ObjectManager`,
  never by string-building alone.
- **`org.bluez.obex`** (session bus, obexd): `Client1.CreateSession` with target `pbap`
  (contacts, `PhonebookAccess1`) or `map` (messages, `MessageAccess1` / `Message1`).
  Details and platform caveats in `SCOPE.md`.
- **Desktop integration**: `QSystemTrayIcon` (maps to StatusNotifierItem on Plasma);
  `org.freedesktop.Notifications` with actions for incoming and active calls and new
  messages, as specified in `SCOPE.md`.

### Module layout (`src/bt_handsfree_kde/`)

- `__init__.py`: `APPLICATION_ID`, `APPLICATION_NAME`, `__version__`.
- `__main__.py`: argument parsing, `QApplication`, qasync event loop, runs `HandsfreeApplication`.
- `app.py`: `HandsfreeApplication`, the only place that wires clients to UI; owns the
  call-duration timer and decides between call notifications and the fallback window.
- `telephony.py`: `TelephonyClient` (**reference implementation** for the coding
  standards), `AudioGateway`, `Call`, call-state constants, `TelephonyError`.
- `bluez.py`: `PhoneInfoClient`, `PhoneInfo` (alias, connected, battery).
- `notifications.py`: `CallNotifier`; action keys; capability check.
- `call_window.py`: `ActiveCallWindow`, shown only when the notification server lacks
  actions or persistence.
- `tray.py`: `HandsfreeTray`; rebuilds the whole menu from state on every update.
- `dbus_helpers.py`: `call_method`, `set_property`, `add_signal_match`,
  `name_has_owner`, `unwrap_variant`, `DBusRequestError`.
- `resources/icon.svg`: bundled application icon, also installed as the hicolor icon.

Clients talk raw D-Bus messages (`call_method`) plus one `AddMatch` rule per interface
and a single message handler, rather than introspected proxies per object: objects come
and go quickly during calls and this avoids racing introspection against removal.

## Coding standards

Enforced in review. They apply to Python, shell scripts, inline helper scripts and (when
they exist) tests alike. When a file is touched, the whole file is brought up to these
standards in the same change.

### 1. Names

- **Descriptive identifiers only.** Never `v`, `arr`, `m1`, `m2`, `acc`, `tmp`, `res`,
  `ctx`, `cfg`, `s`, `t`, `df` or any other abbreviation. Loop variables included:
  `for call_path in call_paths`, not `for p in paths`.
- A name says what the value *is* for the reader (`gateway_address`,
  `calls_by_path`, `retry_count`), not its type or position.
- An accumulator is named after what it becomes for the caller (`menu_entries_to_show`),
  not after its shape (`result`, `out`, `items`).
- A constant is named after its meaning and role (`TELEPHONY_BUS_NAME`,
  `INCOMING_CALL_NOTIFICATION_TIMEOUT_MS`), never after its value or a placeholder.
- The only accepted single-letter names: a mathematical index inside a formula, and `_`
  for a value that is intentionally unused.

### 2. Statements

- **One value per line.** Anything that has to be computed before it is used gets its
  own named variable first: a slice bound, a count, a flag used in a condition
  (`is_incoming = …`, then `if is_incoming`), a string that goes into a message
  (`caller_label = f"{caller_name} ({caller_number})"`).
- **No long one-line call chains.** A chain of calls, a comprehension inside a call, or a
  nested expression is broken into named intermediate steps on separate lines. Prefer
  an explicit loop over a comprehension that needs more than one clause.
- No arithmetic or calls inside subscripts, conditions or f-strings beyond a plain name
  or attribute.

### 3. Layout inside a function

- **Blank lines separate logical steps:** setup, blank line, the loop or main
  computation, blank line, the return or raise. Inside a loop body, "prepare this
  iteration's inputs" is separated from "do the work". A `return` is never glued to the
  last line of a loop.
- **A long function made of semi-independent pieces gets a comment above each piece**
  stating what the piece does and why it sits there (ordering constraints, what it
  protects against). The comment opens the block, the blank line closes it. This is
  preferred over splitting into helpers when the pieces share state and only ever run
  in this sequence.

### 4. Documentation in code

- **Every function and method has a Google-style docstring.** First line: one sentence
  saying what it does. Then, when applicable: a short paragraph of behaviour a caller
  must know (ordering guarantees, empty-input behaviour, what is retried, what is
  destructive), an `Args:` block describing each parameter, a `Returns:` block
  describing the output, a `Raises:` block naming each exception type and when it
  happens. Docstrings describe inputs, outputs and guarantees, not the implementation.
- **Every setting has a comment directly above it** saying what it controls, its unit
  where one applies, what changing it does, and what an empty value or the default
  means. **Every module-level constant** gets the same.
- Classes have a docstring stating what one instance represents or manages.

### 4b. Shell scripts (`scripts/*.sh`)

The same standards, translated: one function per check or action with a header comment
stating purpose and arguments (bash has no docstrings); constants at the top, each with a
comment; run-wide state variables declared and commented in one place; `local` variables
with descriptive names; one action per line — no `cmd && ok || fail` chains, use
`if/else`; blank lines between logical steps; a `main` at the bottom that lists the steps
in order.

### 5. Types, tooling, configuration

- Type hints on every function signature. A function that only raises is typed `NoReturn`.
- ruff `format` and `check` must be clean; settings are in the root `pyproject.toml`.
- Qt slots and async D-Bus handlers are typed like any other function; a `Slot`
  decorator does not replace the docstring.
- Configuration values (if any are introduced) come from one settings module only. A new
  setting is added in the same change as the code that reads it, with its comment.
- No placeholder code, no unused dependencies (see "No placeholder code" above).

### Reference shape

```python
async def answer_call(self, call_path: str) -> None:
    """Answer the call at `call_path` on its audio gateway.

    Answering an already active call is a no-op on the phone side; PipeWire raises
    `org.pipewire.Telephony.Error.InvalidState`, which is surfaced as `TelephonyError`.

    Args:
        call_path: D-Bus object path of the call, as delivered by `InterfacesAdded`.

    Raises:
        TelephonyError: the gateway rejected the request or has gone away.
    """
    call_proxy = self._call_proxies.get(call_path)
    is_known_call = call_proxy is not None

    if not is_known_call:
        raise TelephonyError(f"unknown call {call_path}")

    try:
        await call_proxy.call_answer()
    except DBusError as dbus_error:
        raise TelephonyError(str(dbus_error)) from dbus_error
```

## Conventions

- Verify each change by running the app against the live phone (host run for logic,
  Flatpak run for sandbox permissions) and state the observed outcome. Whether and when
  automated tests are introduced is decided when the D-Bus client layer is stable.
- Runtime and BaseApp versions in the Flatpak manifest are pinned. Bumps are deliberate
  changes with their own commit.
