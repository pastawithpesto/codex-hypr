# Codex Hypr

**A source snapshot of a computer-use fix for Codex on Omarchy Linux.**

The stock app [doesn't currently support native Computer Use on Linux](https://learn.chatgpt.com/docs/linux/linux-app#compatibility-and-limitations).
This fix adds local desktop control through Hyprland: seeing the screen,
clicking, typing, editing text and keyboard navigation.

Developed and independently cross-checked in Codex using
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) and
[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
Tested on Omarchy Linux using a simple test field.

It currently works through a local service used by Codex tasks; the connection
to built-in Computer Use is still unfinished.

Based on [OpenAI Codex **0.163.0**](https://github.com/openai/codex/tree/rust-v0.163.0).
Snapshot version: **0.1.0-dev**. Service version: **0.2.2**.

## Review the changes

Visit the [original Codex repository](https://github.com/openai/codex) and compare
this snapshot with the exact **0.163.0** base. The [diff guide](docs/codex-hypr-comparison.md)
shows how to review every change, including uncommitted local files.

[Try the fix](docs/omarchy-quickstart.md) ·
[Exact base commit](CODEX_HYPR_BASE.json) ·
[Codex license](LICENSE) · [Service license](hyprland-service/LICENSE)
