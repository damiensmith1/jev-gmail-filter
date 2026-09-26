# jev-gmail-filter

Open-source, self-hosted Gmail filter driven by TypeSafe's **Jev**, built
on the [`jevfilter`](https://github.com/damiensmith1/jevfilter) library.
Users define **topics** in plain English (`topics/*.yaml`, jevfilter
topic format); jevfilter judges each email against them; this app labels
emails in Gmail and, for tracked topics, stores **items** with a status
pipeline and stale detection. Job search is the first example topic, not
the product.

No code yet: the app is being built from scratch from `docs/`. An
earlier jobs-only prototype was discarded. Claude Agent SDK summaries are
future work, not core.

## Context

- **Judging:** the `jevfilter` library (PyPI) owns topics, Jev
  questions, thresholds, item matching and tracking rules. The app owns
  Gmail, candidate extraction, SQLite and UI, and never calls Jev
  directly. Features the app still needs from jevfilter are listed in
  `docs/design.md` → "Depends on jevfilter"; build them there, not here.
- **Docs:** <https://docs.typesafe.ai> — append `.md` to any page path for
  its Markdown source.
- Project docs: `docs/background.md`, `docs/requirements.md`,
  `docs/design.md`, plus anything else under `docs/`.

## Commands

None yet. Add them here as the CLI, UI and test setup are built (app
package / CLI name is an open question in `docs/design.md`).

## Conventions

- Python, local, single user. Open source: keep setup simple and
  documented; no personal data, paths or keys in the repo.
- Topics are configuration, not code — never hard-code a topic.
- `TYPESAFE_API_KEY` and Gmail OAuth credentials/tokens live in `.env` /
  local files that are gitignored. Never log or commit them.
- Live Jev calls cost money. Unit tests use jevfilter's `FakeJudge`;
  never add a CI step that calls the real API.
- Classify each Gmail message once (keyed by message ID).
- Commits are atomic and explain *why*. No co-author trailers.

## Keeping docs in sync

Everything under docs/ is this project's source of truth, not a one-time
snapshot — including any file added there after initial setup, not just
background.md/requirements.md/design.md. In the SAME turn as a code
change (not a followup), update the relevant doc when you:
- resolve or add an open question in design.md
- make or change an architecture/approach decision
- add, change, or drop a requirement or non-goal
- learn something that changes the "why" in background.md
- create a new doc under docs/ for a topic that doesn't fit the above

Don't fabricate a decision that wasn't actually made. If it's unclear
whether something is doc-worthy, ask instead of guessing.
