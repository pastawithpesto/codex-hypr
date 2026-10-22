# Private text driver

Based on https://github.com/atx/wtype at commit
`4b9cc29664f178cf380ccfe7f98405d38dffda3c` (MIT; see LICENSE).
The system wtype is not changed or replaced.

This source uses printable physical key codes for literal text. Chromium can
otherwise interpret punctuation using a physical key's special meaning.

Local changes add two optional inherited pipe descriptors:
`LINUX_CUA_STEP_FD` and `LINUX_CUA_STEP_ACK_FD`. The host authorizes each text
key pair with `T`; the driver first sends `R` only after all keyboard/keymap
preparation for that pair is complete, then waits for `T`. The parent waits
for `R`, refreshes target/grant checks, and only then sends permission.
The driver acknowledges with `D` only after press, release,
and compositor roundtrips. EOF or any other permission byte stops the driver.
The text handshake accepts only literal text, not arbitrary key/modifier commands. These
pipes connect the service to its own child process; they do not cross or alter
the OpenAI runtime's sandbox.

Local checks reject failed Wayland initialization, keymap and press/release
roundtrips before acknowledging a character. A release is attempted after a
failed press roundtrip. The child uses C.UTF-8 and rejects unavailable locales.
After destroying its virtual keyboard, the driver waits for a checked compositor
roundtrip before successful exit. This establishes that removal was processed;
it does not establish the cause of a later application's navigation failure.

The separate `--host-key` mode accepts only the service's fixed navigation
allowlist and requires both host pipes. It creates a complete evdev/pc105/us
XKB map for its own virtual keyboard, independently of the current seat or
IME keymap. Navigation uses the same ready/check/permission handshake.
One host `T` permits the requested key pair and its fixed optional
Ctrl/Shift modifier. Checked roundtrips confirm modifier setup, press, release
and modifier reset before `D`. Failure attempts both key and modifier release
and emits no acknowledgement. No global keyboard configuration is changed.

Build with `python3 build-driver.py`. Requires cc, pkg-config,
wayland-scanner, wayland-client and xkbcommon development files.
