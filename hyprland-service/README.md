# Linux computer-use service

Service version **0.2.2**, included in the Codex Hypr source snapshot.

A user-managed local desktop broker for Hyprland/Omarchy, written in Python's
standard library. It provides observation, window focus, coordinate clicks,
bounded printable text input, and scoped navigation keys through a private Unix socket. The backend
uses `hyprctl`, `grim`, and a private source-built Wayland text driver with this
machine's Lua dispatcher API. The system wtype is not modified.

The service is an independent integration. It is **not** an OpenAI native CUA
replacement, does not impersonate its host broker, and does not change the
installed runtime, sandbox, or app permissions. Clients use the ordinary
task's permitted shell path. There is no TCP listener or socket injection into
a restricted CUA worker.

## Install and run

Requires Linux, Python 3.11+, a running Hyprland session, and installed
`hyprctl`, `grim`, and `systemctl`. Building the included MIT text driver needs
cc, pkg-config, wayland-scanner, wayland-client and xkbcommon development files.
No pip dependencies or root access.

```sh
python3 build-driver.py
python3 install-user-service.py
systemctl --user start linux-computer-use.service
linux-cua ping
```

The installer copies runtime code and its private driver into an isolated
release under `~/.local/share/linux-computer-use-service/releases/`, with a
stable `current` symlink. It also provides `~/.local/bin/linux-cua`, which works
from other directories and writes client images into the caller's workspace.
Moving or removing the development checkout does not remove the installation.
Run the installer again after changing source to deploy a new release, then
restart the unit. Old release directories are retained for recovery.
Use `linux-cua ping` or `linux-cua --help`; its subcommands match the module CLI.

To start the service automatically with the graphical session:

```sh
systemctl --user enable linux-computer-use.service
```

The installer itself does not enable automatic startup. Starting the
service creates its private socket; it does not access the desktop. Desktop
requests fail until the local owner creates a session grant.

The socket is `$XDG_RUNTIME_DIR/linux-computer-use/control.sock` (mode 0600).
Its parent directory is mode 0700. Peer credentials must match the service's
UID. The systemd unit permits only AF_UNIX sockets and confines persistent
writes to its runtime directory. A private temporary directory supports wtype's
temporary keymap files. The service stops with the graphical session.

## Stop or remove

Stop the service and disable automatic desktop-session startup:

```sh
systemctl --user disable --now linux-computer-use.service
```

To remove it completely, delete only the installer's paths:
`~/.local/bin/linux-cua`,
`~/.config/systemd/user/linux-computer-use.service`,
`~/.config/systemd/user/linux-computer-use.service.previous` (if present), and
`~/.local/share/linux-computer-use-service/`.
Then run `systemctl --user daemon-reload`.

## Grant one scoped session

The local owner grants actions explicitly. `observe` authorizes **whole-desktop
screenshots and window inventory**, including content in other apps.
`--app-class` limits the apps that may receive input, using exact Hyprland
classes from a current authorized inventory. It is not screenshot isolation.

Open the app you want to use. Run `hyprctl -j clients` to find its exact class,
then replace `EXACT_APP_CLASS` below. Grant creation itself does not access the
desktop:

```sh
linux-cua authorize --actions observe focus click type_text press_key \
  --app-class EXACT_APP_CLASS --ttl 300 \
  --label fixture-test --credentials-out ./fixture.credentials.json
linux-cua observe --credentials ./fixture.credentials.json --images-dir ./captures
```

Inspect the PNG returned by `observe`. Use its exact window address and
observation ID. For each next operation use the fresh observation returned by
the previous action, inspect its PNG, then supply `--reviewed`:

```sh
linux-cua focus --credentials ./fixture.credentials.json \
  --observation OBSERVATION_ID --window WINDOW_ADDRESS --reviewed
linux-cua click --credentials ./fixture.credentials.json \
  --observation OBSERVATION_ID --window WINDOW_ADDRESS --reviewed \
  --x SCREENSHOT_X --y SCREENSHOT_Y
linux-cua press_key --credentials ./fixture.credentials.json \
  --observation OBSERVATION_ID --window WINDOW_ADDRESS --reviewed --key 'Ctrl+A'
printf '%s' 'CONTROL_INPUT_TEST' | linux-cua type_text \
  --credentials ./fixture.credentials.json --observation OBSERVATION_ID \
  --window WINDOW_ADDRESS --reviewed
```

Capitalized placeholders need values from the current screenshot/receipt.
Use a unique credential filename for each task; authorization never replaces an
existing secret file. If a delegated task's sandbox makes the runtime grants
directory read-only, grant creation needs that task's normal command approval
flow or an authorized host-side owner. A successful `ping` does not establish
permission to create a grant. Do not change sandbox settings to resolve this.
`--reviewed` is an assertion from the controller; the service cannot prove that
an agent actually inspected an image. Client images are written in the
caller's workspace; they do not require access to the daemon's storage.

The CLI prints only the credential file path, never the token. The credential
file is a bearer secret and must remain mode 0600. It may be shared only with
the intended local controller. Grants last at most one hour (default five
minutes), are checked again during input, and can be revoked immediately:

```sh
linux-cua revoke SESSION_ID
systemctl --user stop linux-computer-use.service
```

Open the intended app before using this API; app launching is outside the
broker's methods. A sandboxed task may lack access to normal browser profile
or session-manager paths even when it can use the broker. Host-side app
preparation is separate from proving that a Codex task can control it.

The task label is descriptive and **does not authenticate an OpenAI turn**.
This service does not receive OpenAI's app approvals or active-turn authority.
Creating a grant must be an authorized local action, and it never overrides
the invoking task's own sandbox or approval policy. It offers no protection
against a process already able to read or modify all files as the host user;
same-UID processes are within the local owner's trust boundary.

## Input behavior and limits

- Observations expire after 90 seconds and are consumed by one input attempt.
- Target identity, window geometry, monitor layout, and focus are rechecked.
- Mouse release runs even if pressing fails. Text is limited to 200 printable
  characters. `press_key` separately allows Tab, Shift+Tab, Escape, arrows,
  Home/End, PageUp/PageDown, Ctrl+A, Backspace and Delete. It requires its own
  grant action and uses a fixed private-driver operation for each key. Navigation
  supplies a complete private keyboard map, so it works independently of the
  current input method's map. Enter/Return, arbitrary
  key chords, shell execution and app launching are outside the API.
- A changed window, expired grant, failed command, or unknown input result
  stops the operation. Do not retry uncertain input; inspect a new observation.
- One persistent virtual keyboard serves each text request. The driver first
  completes keyboard preparation and signals readiness. The host then checks
  the current grant and target before authorizing input. Separate host
  pipes authorize one character at a time and acknowledge a complete
  compositor press/release pair. This avoids creating a new virtual keyboard
  for every character. The driver uses printable physical codes so punctuation
  is not assigned to control keys. See `vendor/wtype/PROVENANCE.md`.
- Full client-set checks can reject an action when an unrelated window changes.
  This is deliberate until a verified obstruction-aware replacement is added.
- Global Wayland input retains a focus-to-input race. Checking before each
  character limits the race but cannot make physical user input exclusive.
  Screenshots may include popups or layers. Per-app screenshot isolation and
  accessibility trees are not implemented.
- Only one desktop operation runs at a time. The socket accepts at most eight
  concurrent connections, requests are bounded to 64 KiB, and captures to
  24 MiB each. Old captures are removed under a 128 MiB retention budget before
  operations. Current-operation output can temporarily exceed that budget.
- Audit entries contain method, session ID, timestamp and outcome. They omit
  text, tokens, titles and screenshots; the log rotates after about 1 MiB.

## Protocol

One UTF-8 JSON-RPC 2.0 request per Unix-socket connection, terminated by a
newline. Responses are newline-delimited JSON with the same ID. Supported
methods are `ping`, `session.status`, `observe`, `focus`, `click`, `type_text`,
`press_key`.
Every method except `ping` requires `params.auth = {session, token}`.

Input parameters are `observation`, `window`, `reviewed: true`; `click` adds
integer screenshot-pixel `x,y`, `type_text` adds `text`, and `press_key` adds one
exact allowlisted `key`. Images are returned
as `{mime_type: "image/png", base64: "..."}`. The CLI saves them as local PNGs
and substitutes paths in its output. No client RPC can issue grants, change
permissions, run commands, or request arbitrary file reads.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Development and independent cross-checks were carried out in Codex using
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) and
[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

The offline suite passes 38 tests. A separate live Omarchy test verified
observation, focus, clicking, Unicode text replacement, editing and field
navigation in an inert native Wayland Chromium test page. The test grant was
revoked and its rejection verified afterward.

See the [verification summary](VERIFICATION.md) for the scope and
limits. Raw screenshots, credentials and private task records are excluded.
The `desktop_tested: false` field in a `ping` response describes that health
check, not the outcome of separate live tests.
