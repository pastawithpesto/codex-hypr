# Try the fix on Omarchy

This installs the local desktop service. Codex tasks can use its `linux-cua`
command. The connection to the app's built-in Computer Use is still unfinished.

## 1. Review the source

Read the [README](../README.md) and [compare the changes](codex-hypr-comparison.md)
with original Codex 0.163.0.

## 2. Build and install

Use Ghostty in your Omarchy Hyprland session. These packages provide the build
tools and screen capture dependency; Omarchy skips packages already installed:

```sh
omarchy pkg add python base-devel pkgconf wayland libxkbcommon grim
```

From the root of this source checkout:

```sh
cd hyprland-service
python3 build-driver.py
python3 -m unittest discover -s tests -v
python3 install-user-service.py
systemctl --user enable --now linux-computer-use.service
~/.local/bin/linux-cua ping
```

A successful `ping` means the service is running. Test actual desktop control
separately, using a blank editor or another disposable test field.

## 3. Allow one task

Open the intended app. Run `hyprctl -j clients` to find its exact `class`, then
replace `EXACT_APP_CLASS` in this command:

```sh
~/.local/bin/linux-cua authorize \
  --actions observe focus click type_text press_key \
  --app-class EXACT_APP_CLASS --ttl 300 \
  --credentials-out ./session.credentials.json
```

The grant expires after five minutes. Observation includes the whole desktop;
the app class limits which app receives input. Keep the credential file private.
If a task cannot create the grant, use its normal command approval flow.

Tell Codex to use `linux-cua` and the grant file for the chosen app. It must
inspect a fresh screenshot before each input and verify the result afterward.
The [command reference](../hyprland-service/README.md#grant-one-scoped-session)
shows observation, focus, click, typing and key examples.

When finished, use the session ID returned by `authorize`:

```sh
~/.local/bin/linux-cua revoke SESSION_ID
rm -- ./session.credentials.json
```

## Stop or remove it

To stop the service and disable desktop-session startup:

```sh
systemctl --user disable --now linux-computer-use.service
```

For a complete removal, delete only these installer-owned paths:

- `~/.local/bin/linux-cua`
- `~/.config/systemd/user/linux-computer-use.service`
- `~/.config/systemd/user/linux-computer-use.service.previous`, if present
- `~/.local/share/linux-computer-use-service/`

Then run `systemctl --user daemon-reload`. The service never replaces system
`wtype` or edits your Hyprland configuration.

## Troubleshooting

- Service stopped: `systemctl --user status linux-computer-use.service`.
- Service errors: `journalctl --user -u linux-computer-use.service -n 30`.
- Missing driver: run `python3 build-driver.py` before installation.
- Session rejected: create a new authorized grant; expired and revoked grants
  cannot be reused.
- Input result uncertain: inspect a fresh screenshot before deciding what to do.

Review logs before sharing them. Do not post credential files, screenshots or
private app contents in an issue.
