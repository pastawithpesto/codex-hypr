# Snapshot verification

Service version: **0.2.2**. Live checks: **22 October 2026 UTC**.

Development and redundant review were carried out in Codex using
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) and
[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

## What was tested

The offline suite passed **38 tests**, covering the local socket, scoped grants,
expiry and revocation, input supervision, key restrictions and installation
independent of the development checkout.

An independent Codex task used the installed service on Omarchy/Hyprland to control
an inert native Wayland Chromium test page. The live checks verified:

- Screen observation, exact-target focus and clicking.
- Text replacement with underscores and Unicode characters.
- Home and Right movement, Delete and character restoration.
- Tab and Shift+Tab between two test fields.
- A fresh scoped grant, revocation, rejection of revoked credentials and cleanup.

The live check displayed `FINAL_READY_你好` exactly, with **14 characters**.
Screenshots were independently inspected in Codex.

## Limits

This validates the local service route through Codex tasks in the test page.
The app's built-in `cua_repl` integration and arbitrary-app compatibility have
not been established. Desktop-session startup is configured; service restart
was checked, but logout and reboot were not tested.

This snapshot excludes credentials, local user paths, private task IDs,
actual app inventories and raw screenshots. Synthetic identifiers in automated
tests are test data.
