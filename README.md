# jev-gmail-filter

[![CI](https://github.com/damiensmith1/jev-gmail-filter/actions/workflows/ci.yml/badge.svg)](https://github.com/damiensmith1/jev-gmail-filter/actions/workflows/ci.yml)

Filter Gmail with plain-English topics. Describe what you care about
("receipts for things I bought", "recruiters contacting me about a role")
and the app labels matching emails, groups them into tracked items (a job
application, an order) with a status, and flags anything that has gone
quiet. Judging is done by [jevfilter](https://github.com/damiensmith1/jevfilter)
with TypeSafe's Jev.

Self-hosted and local: your email, topics and database stay on your machine.
Only your Primary inbox is read.

> Early development: works end to end, not yet battle-tested on many inboxes.

## Setup (about 15 minutes, once)

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), a TypeSafe API key,
and a Google account.

```sh
git clone https://github.com/damiensmith1/jev-gmail-filter
cd jev-gmail-filter
uv sync
uv run jev-gmail-filter ui
```

Your browser opens on a setup wizard (prefer the terminal? `uv run
jev-gmail-filter init` asks the same questions). It walks you through:

1. your TypeSafe API key (saved to `.env`, gitignored)
2. **your own Google OAuth client**: step-by-step instructions for Google
   Cloud Console. Everyone who uses this app creates their own, so no one
   else's app ever sees your mail
3. signing in to Gmail in your browser
4. picking starter topics (Jobs, Receipts) to edit later
5. how far back to scan, with the estimated cost, and a dry run first if
   you like (judge without labelling)

Your files live in `./data` (gitignored): the OAuth client and token, your
topics, and the SQLite database.

## Use

In the web UI (`uv run jev-gmail-filter ui`): an overview with **Sync
now**, the **Review** queue, your tracked **Items** as a board per status,
recent **Emails**, **Topics** (edit, add, rescan) and **Settings** (labels,
spend cap, auto-sync, sign out).

Or from the terminal:

```sh
uv run jev-gmail-filter sync          # judge new mail (spend cap: --max-usd, default $1)
uv run jev-gmail-filter watch         # keep syncing every 5 minutes
uv run jev-gmail-filter review        # emails that need your decision
uv run jev-gmail-filter review 3 yes  # ...and resolve one
uv run jev-gmail-filter items         # tracked items and their status
uv run jev-gmail-filter items --stale # the ones that have gone quiet
uv run jev-gmail-filter labels        # after a dry run: turn labels on
uv run jev-gmail-filter status        # setup and total spend
```

## Topics

A topic is a YAML file in `data/topics/`. Only a name and a description
are required:

```yaml
name: Receipts
description: Receipts, invoices and order confirmations for things I bought.
```

Add categories, fields, tracking and more; see
[topics/examples/](topics/examples/) and the
[jevfilter topic format](https://github.com/damiensmith1/jevfilter/blob/main/docs/topic-format.md).
Editing a topic makes older mail eligible to be judged again
(`sync --since-days N`).

## Cost

Jev bills input tokens only. A typical email costs a few hundredths of a
cent; `init` estimates the backscan before it starts, and every sync has a
spend cap.

## Development

```sh
uv run pytest                                   # no network: fake Gmail, fake Jev
uv run ruff check . && uv run ruff format .
```

See [docs/](docs/) for background, requirements and design, and
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

MIT
