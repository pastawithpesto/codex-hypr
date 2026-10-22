# Compare Codex Hypr with original Codex

Codex Hypr is a source snapshot of a computer-use fix for Codex on Omarchy Linux.
Start with the original [OpenAI Codex repository](https://github.com/openai/codex)
and review the snapshot's changes before installing or running it.

## Exact upstream base

The source base is **OpenAI Codex 0.163.0**:

- Release tag: [`rust-v0.163.0`](https://github.com/openai/codex/tree/rust-v0.163.0).
- Commit: [`83086188ae0614bd882818ff1aa23f3194962012`](https://github.com/openai/codex/commit/83086188ae0614bd882818ff1aa23f3194962012).
- Machine-readable record: [CODEX_HYPR_BASE.json](../CODEX_HYPR_BASE.json).

Use this pinned commit for comparison. Comparing with upstream's moving `main`
branch mixes snapshot changes with unrelated upstream changes.

## Compare a committed snapshot

Run these commands inside a checkout of Codex Hypr. Fetch the base tag directly
from the original repository, then inspect the resolved commit:

```sh
git fetch https://github.com/openai/codex.git refs/tags/rust-v0.163.0
git rev-parse 'FETCH_HEAD^{commit}'
```

The result should be `83086188ae0614bd882818ff1aa23f3194962012`.
Then review the summary, changed-file list, complete diff and snapshot commits:

```sh
codex_upstream_base=83086188ae0614bd882818ff1aa23f3194962012
git diff --stat "$codex_upstream_base" HEAD
git diff --name-status "$codex_upstream_base" HEAD
git diff "$codex_upstream_base" HEAD
git log --oneline "$codex_upstream_base"..HEAD
```

To inspect just the Hyprland backend's source:

```sh
git diff "$codex_upstream_base" HEAD -- hyprland-service/
```

These commands compare the committed source in the checked-out revision.

## Include uncommitted snapshot files

If the snapshot files are uncommitted, inspect tracked-file edits and untracked
files too:

```sh
git status --short
git diff "$codex_upstream_base"
git ls-files --others --exclude-standard
```

The last command lists new files; it does not show their contents. Open those
files as part of the review. A plain `git diff` does not include untracked
files, and comparing the base with `HEAD` omits all uncommitted changes.

## Snapshot contents

- Codex Hypr identification, fix version and exact upstream provenance.
- `hyprland-service/` version 0.2.2, including its
  source, build/install scripts, tests and third-party license notices.
- Omarchy setup instructions, verification results and this comparison guide.

The upstream Rust CLI source is unchanged in this snapshot. The Hyprland service is
included alongside it; native `cua_repl` registration and native app/turn
authorization remain unfinished. Backend test results do not establish that
the bundled ChatGPT desktop runtime has been integrated into this snapshot.
