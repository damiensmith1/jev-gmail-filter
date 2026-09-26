---
title: Design
tags: [jev-gmail-filter, design]
status: draft
---

# Design

See [background](background.md) for why and [requirements](requirements.md) for what.

## Architecture

```
Gmail API ──poll──▶ ingest ──▶ candidate extraction (app code)
                                   │
              topics/*.yaml ──────▶│
                                   ▼
          jevfilter  Filter.judge(Content(email, candidates))
             membership · category · fields · flags · scores (one Jev request)
                                   │
                                   ▼
          jevfilter  match_item(email, topic, items)   (tracked topics only)
          jevfilter  track.next_status / is_stale      (pure rules, no Jev)
                                   │
                        ┌──────────┴──────────┐
                        ▼                     ▼
                     SQLite              Gmail labels
                        │
                        ▼
                   local web UI  (topics · items · review · emails)
```

The split:

- **[jevfilter](https://github.com/damiensmith1/jevfilter)** (library, on
  PyPI) owns everything semantic: loading and validating topics, turning
  them into Jev questions, packing requests, thresholds and the review
  band, item matching, tracking rules, spend caps and failure policy. It
  is stateless and knows nothing about email.
- **This app** owns everything else: Gmail (OAuth, polling, labels),
  candidate extraction from emails, SQLite storage of emails / results /
  items / reviews, the setup flow and the web UI.

The app never calls `typesafe-sdk` directly and never builds questions.

## Topics

One YAML file per topic in `topics/`, in the
[jevfilter topic format](https://github.com/damiensmith1/jevfilter/blob/main/docs/topic-format.md).
Loaded with `Topic.load("topics/")`; adding a topic means adding a file
(or using the UI, which validates with `Topic.from_dict` and writes with
`to_yaml`). Only `name` and `description` are required.

App-only settings live under `meta`, which jevfilter ignores and excludes
from the topic version:

| `meta` key | Meaning |
|------------|---------|
| `gmail_label` | Gmail label for the topic. Omit to use `name`; `false` to disable labels. Categories become `<label>/<category>`. |

**The job-search example that ships with the repo:**

```yaml
name: Jobs
description: >
  My own job search: applications I submitted, and recruiters, HR or
  hiring teams contacting me about a specific role.
exclude: Job alerts, recommendation digests, newsletters, marketing.

categories:
  applied: Confirms I submitted an application.
  recruiter: A recruiter or hiring manager reaches out about a role.
  hiring_contact: Other HR / hiring-team mail about a role — scheduling, follow-ups, info requests.
  interview: Invites me to an interview or phone screen, or confirms/reschedules one.
  assessment: Asks me to complete a take-home, coding test or online assessment.
  rejection: Tells me I'm not moving forward.
  offer: Extends an offer or discusses offer terms.

fields:
  company: {kind: org, about: "The hiring company, not a job board or ATS.", required: true}
  role: {kind: title, about: The job title.}

track:
  match_on: [company]
  statuses:
    contacted: [recruiter]
    applied: [applied]
    assessment: [assessment]
    interviewing: [interview]
    offer: [offer]
  terminal:
    rejected: [rejection]
  stale_after_days: 21

meta:
  gmail_label: Jobs
```

`required: true` on `company` means an email where Jev picks "none of
these" goes to review instead of creating a nameless job.

Status rules (forward-only pipeline, terminal states, categories that link
without moving status, staleness) are jevfilter's `track` functions; see
its design doc. The app stores the resulting status.

### Versions and rescans

Each jevfilter result records the topic `version` (hash of everything but
`meta`) and the library's `wording_version`. An email is judged once per
(email, topic version). When a topic's version changes, the UI offers a
rescan over a chosen window, with a cost estimate from `Filter.explain`.
Editing `meta` (e.g. the label) never triggers a rescan.

## Judging an email

State is the email: `from`, `subject`, `date`, `body` (trimmed).

1. **Candidates (app).** For each topic field, extract candidate values
   from the email by `kind`:
   - `org`: sender name, sender domain, ATS subdomains, capitalised
     phrases after "at / to / from"
   - `title`: phrases ending in a role word
   - plus values already on that topic's items, but only those the email
     mentions (first real run: with 16 tracked jobs, unmentioned known
     companies filled the 15-candidate cap and crowded out the email's own
     company, sending 43 of 104 emails to review)

   Passed to jevfilter as `Content(email, candidates={topic: {field: [...]}})`.
   A value that isn't extracted can't be chosen, so the app tracks how
   often "none of these" wins per field.
2. **Judge (jevfilter).** `Filter.judge` asks every topic's questions in
   one Jev request and returns, per topic, `match` / `review` / `no`, the
   category, fields, flags, scores and reasons.
3. **Item (tracked topics).** If the Gmail thread is already linked to an
   item in this topic, use it (no Jev call). Otherwise
   `match_item(email, topic, items)`: code pre-filters items by
   `match_on` fields, and Jev picks which remaining item it is, or "new".
4. **Apply (app).** Store the result, link or create the item, update its
   status via `track.next_status`, write Gmail labels, queue reviews.

Backscans use `AsyncFilter.judge_many` with a concurrency limit and a
shared `Budget` so a large window can't overspend.

## Confidence handling

jevfilter's `ThresholdPolicy` defaults, overridable per topic in the topic
file (`thresholds:`):

- membership ≥ 0.7 → match; < 0.3 → no; otherwise review
- a used category / field with confidence < 0.5 → review
- a required field answered "none of these" → review
- item match with low confidence → review

Each review carries jevfilter's machine-readable reasons
(`membership_uncertain`, `field_missing:company`, …). Review items are
resolved in the UI; the user's decision is stored alongside the raw
result (useful later for tuning thresholds or topic wording).

## Data model (SQLite)

- `topics` — name, version, loaded from `topics/*.yaml`
- `emails` — gmail_id (PK), thread_id, sender, subject, received_at,
  snippet
- `results` — gmail_id, topic, topic_version, wording_version, outcome,
  and the jevfilter `TopicResult.to_dict()` JSON (probabilities, category,
  fields, reasons); plus per-request model, request ID and cost
- `items` — id, topic, fields (JSON), status, stale, created_at,
  last_email_at, notes, source (email / manual)
- `item_emails` — item_id, gmail_id, category
- `review` — gmail_id, topic, reasons, resolved, user decision (JSON)
- `meta` — last sync time / Gmail historyId, setup state

## Setup flow (first run)

`jev-gmail-filter init` or the first screen of the web UI:

1. Check for `TYPESAFE_API_KEY`; prompt and write `.env` if missing. The
   app loads `.env` itself (jevfilter never reads files).
2. Walk the user through creating their own Google OAuth client (see
   [Google access](#google-access)), then open the browser to authorise
   and save the token.
3. Pick starter topics: bundled examples (Jobs, Receipts, Travel, …) or
   blank.
4. **Choose the backscan window** (e.g. 1 day / 1 week / 2 weeks / 1 month
   / custom), with an estimated email count and Jev cost.
5. Dry run option: classify without writing Gmail labels, review results,
   then enable labels.

## Google access

Reading and labelling mail needs the `gmail.modify` scope, which Google
classes as **restricted**. A single OAuth client shared by every user
would have to pass Google's restricted-scope verification (verified
domain, privacy policy, demo video, scope justification) before anyone
outside a hand-picked test list could use it.

So **everyone brings their own OAuth client**, the maintainer included.
Each user is then the only user of their own Google app, which Google
treats as personal use: no verification, and no one else's credentials
involved. The cost is a one-time, few-minute setup, which `init` and the
UI wizard walk through:

1. **Create a project** at <https://console.cloud.google.com> (any name).
2. **Enable the Gmail API** for it (APIs & Services → Library → Gmail API).
3. **Create a Desktop client**: Google Auth Platform → Clients → Create
   client → *Desktop app* → Download JSON, and give it to the app (upload
   in the UI, a path in `init`; copied into `data/credentials.json`,
   gitignored). If Google first asks to configure the app, only a name,
   the user's email and audience *External* are needed; logo, home page,
   privacy policy and domains stay blank.
4. **Sign in**: the browser opens Google's consent page. Google shows
   "Google hasn't verified this app" because it's the user's own
   unverified app: *Advanced → Go to (app name)*, then allow. The token is
   saved as `data/token.json` (gitignored) and refreshed automatically.

The app stays in Google's **Testing** mode. That's enough: the project
owner can sign in without being listed as a test user (another account
must be added under Audience → Test users), and the only cost is signing
in again every 7 days. Publishing to "In production" would remove the
7-day limit, but Google requires a home page URL, a privacy policy URL and
an authorized domain to publish, even without verification, so it's not
part of setup.

`init` and the wizard check each step they can (the file is a Desktop
client, the Gmail API responds, the granted scope is `gmail.modify`) and
say which step to revisit when something fails. Removing access later:
revoke it at <https://myaccount.google.com/permissions> and delete
`token.json`.

**Later, possibly:** a shared, Google-verified client so users can sign
in with one click and skip the setup. Because all data stays on the
user's machine, Google's paid security assessment shouldn't apply, but
verification still takes paperwork and weeks of review. Not worth it
right now; the app reads whichever client file it's given, so switching
later is a configuration change, not a rewrite.

## Code layout

Package `jev_gmail_filter` (CLI `jev-gmail-filter`):

- `config.py` — data folder (`./data` or `$JGF_DATA_DIR`), `.env` loading
- `mail.py` — the `Email` model, parsing Gmail messages (plain text preferred, HTML → text)
- `gmail.py` — `MailSource` interface; `GmailSource` (sign-in, search,
  history, labels); setup checks with fix-it messages
- `candidates.py` — candidate values by field kind (`org`, `title`, `email`)
  plus values already on the topic's items
- `db.py` — SQLite store (schema above)
- `pipeline.py` — email → jevfilter (staged) → items / statuses → storage,
  labels, review queue; sync; stale refresh; resolving reviews
- `onboarding.py` — the setup steps, shared by `init` and the UI wizard
- `topic_form.py` — topic ↔ editor form (pure, round-trips exactly), starters
- `cli.py` — `ui`, `init`, `sync`, `watch`, `review`, `items`, `labels`,
  `categories`, `status`
- `web/` — the web UI (below): `app.py` (routes, local-only middleware),
  `runtime.py` (background scans, sign-in, auto-sync timer, email cache),
  `views.py` (page data, no HTTP), `templates/` (Jinja), `static/`
  (`app.css` with the design tokens, a small `app.js`)

`topics/examples/` ships sample topics; `init` copies the chosen ones into
`data/topics/`, where the user edits them.

## Sync behaviour

- First run (or `sync --since-days N`): a date-based search of the Primary
  inbox, processed oldest first so item statuses move forward in order.
- After that: Gmail history since the saved `history_id`, keeping only
  Primary-tab messages. If the saved id has expired, fall back to a date
  scan from a day before the last sync.
- The saved position only advances when a sync finishes. A sync stopped by
  the spend cap, a Jev error or `--limit` is simply resumed next time;
  already-judged mail is skipped (judged once per topic version).
- Judging uses jevfilter's staged mode (`speculative=False`): most mail
  matches no topic, so membership is asked first.
- Labels: a match gets `<label>` and `<label>/<category>`. Labels can be
  off (dry run); `labels` turns them on and labels past matches. A failed
  label write is reported, never loses the judgment.
- Gmail rate limits: Gmail allows 6,000 quota units per user per minute,
  and fetching a full message costs 20, so a fast backscan (~300+ emails a
  minute) hits it. A client-side pacer charges each call its cost and waits
  before any call that would push the last minute over 5,000 units (about
  250 emails a minute), so syncs stay under the limit. As a backstop, every
  request retries rate-limit and 5xx answers with exponential backoff (up to
  ~2 minutes); if Gmail still refuses, the sync stops cleanly and resumes
  next time.
- Re-check: the review queue can be judged again with the current code
  and topics (UI button, `review --recheck`), e.g. after a topic edit.
- Reviews: uncertain membership → a "topic" review (yes / no); an
  uncertain item match → an "item" review (item id / new). Resolving applies
  the same labelling and tracking as an automatic match.

## Web UI

`jev-gmail-filter ui` serves a local web app on 127.0.0.1 and opens it
in the browser. It's **FastAPI + server-rendered Jinja templates + one
CSS file**, with a few lines of plain JavaScript (live scan progress,
sign-in polling, auto-submitting selects, the file drop zone, j/k list
navigation). No Node toolchain: the open-source setup stays `uv sync`.
Every action is a normal form POST that redirects back, so the pages work
without JavaScript.

### Design language: "Sage"

Calm and minimal, light, balanced density. Tokens (CSS variables in
`static/app.css`): ground `#F5F7F5`, surface `#FFFFFF`, line `#DDE5E0`,
ink `#152019`, muted `#52605A`, one accent deep teal `#0E6B5C`, warn
`#94560F`; radius 6 px. Type: Instrument Sans for UI and headings, Geist
Mono for numbers, times, costs and metadata. The fonts are meant to be
bundled in `static/fonts/` (both OFL), never loaded from Google, so the
app makes no third-party requests; until they're added the CSS falls back
to the system sans and monospace. Mockups: the "jev-gmail-filter design
language" canvas (Explorations: three layouts × four directions; Chosen
direction: the screens below in Paper and Sage; Sage chosen).

### Layout

The inbox shell: a side nav (Overview, Needs you with a count, Everything,
Items; topics with match counts; sync status, spend and **Sync now**;
Settings, flagged "dry run" while labels are off), the main column, and a
right-hand context pane.

- **Overview** (home) — greeting and what's new; stat tiles (needs you,
  new this week out of all judged, tracked items, gone quiet); the Needs you queue with
  one-click answers; a card per topic with 14-day activity bars; latest
  matches. Pane: **Today** — replies you owe (matches whose reply-flag is
  yes, when a topic has one), items gone quiet, items that moved this week.
- **Needs you** — the review cards. Pane: the email itself (fetched from
  Gmail), Jev's confidence against the topic's thresholds, what it would
  do on "yes", and the answer (Yes / No, or which item / new).
- **Everything** — all judged mail with filters (all, matched, needs you,
  not in a topic, per topic), search, paging. Pane: the verdict per
  matched topic (category, details, flags, labels), other topics' scores,
  **Not &lt;topic&gt;? Remove it** (a correction: labels removed, email
  detached from its item, result marked `user_corrected`), Open in Gmail.
- **Items** — secondary view: per tracked topic, a board (a column per
  pipeline status; closed statuses behind a toggle) or a list. Pane: the
  item (stage progress, linked emails, notes, status change), or "add an
  item by hand".
- **Topics** — cards (description, what it pulls out, tracking, label,
  matches, last match, reviews) with Edit / See emails / Delete; **New
  topic** from blank, a starter or an example; rescan. The **editor** is
  the plain-words form (`topic_form.py`), round-tripped through the
  server on every action (add / remove rows, apply YAML, Try it), so it
  needs no client state. Problems block Save; an unchanged Save leaves the
  file untouched; a changed topic offers a rescan. Pane: **Try it** on a
  recent or pasted email.
- **Settings** — labels (turning on labels past matches), spend cap per
  sync, auto-sync interval, Gmail categories (with counts on request),
  account and sign out.
- **Setup** (until done) — a stepper and one card per step: API key, the
  Google client (numbered steps, Console link, drop the JSON), Sign in
  with Google (runs in the background while the page waits), topics,
  backscan window with count and cost estimate, dry run by default.

### Behaviour

- Scans (first scan, Sync now, Rescan, Re-check, auto-sync) run one at a
  time on a background thread (`Runtime`); every page shows live progress,
  then a result banner until dismissed.
- Auto-sync runs in the server (every N minutes from Settings) while the
  app is open; after a failed or partial attempt it waits a full interval.
- Security: the app can read Gmail, so it answers only requests addressed
  to localhost (blocks DNS rebinding) and refuses POSTs whose Origin or
  Referer is another site (blocks other websites driving it).
- Item history (`item_events`: created, status, manual) feeds "moved this
  week" and the item timeline.

## Status

Milestone 1 (CLI end to end) and Milestone 2 (web UI with the setup
wizard, rebuilt on FastAPI in the Sage design) are built. Web tests drive
the wizard and every page and action through FastAPI's test client; every
page also renders against a real data folder. Tested with a fake Gmail and a scripted Jev (no network); checked end
to end against real Jev with a fake inbox (6 realistic emails: all
classified correctly, the follow-up linked to the right job, $0.00036).
Not yet run against a real Gmail account.

## Depends on jevfilter

Everything the app needs from jevfilter is built (jevfilter 0.2.0–0.4.0):
topics, `Filter.judge` / `explain`, thresholds and reasons, failure
policy, serialisable results, `match_item`, `track.*`,
`AsyncFilter.judge_many`, `Budget`, and request packing / splitting.
The app should depend on `jevfilter>=0.4` (0.4 adds staged judging,
which cuts cost when most mail matches no topic, and category examples).

## Decisions

- Generic Gmail filter; jobs is one topic, not the product.
- Open source, self-hosted, Python, SQLite. Licence: MIT.
- An app, not a library: runs from a clone; not published to PyPI.
- Judging core lives in the `jevfilter` library (own repo, on PyPI); this
  app is its first consumer and owns Gmail, storage and UI. The app never
  calls Jev directly.
- Topics use the jevfilter topic format; app-only settings go under
  `meta`.
- Candidate extraction for email lives in the app (email-specific).
- Topics defined as plain-English YAML in `topics/`, editable in the UI.
- Backscan window chosen by the user during setup.
- Web UI: FastAPI + Jinja + plain CSS, replacing an earlier Streamlit UI,
  which couldn't match the chosen design (layout, spacing and widgets are
  Streamlit's). Server-rendered keeps the project Python-only.
- Design language "Sage" (chosen from four directions and three layouts):
  the inbox shell with Overview as home; the board is a secondary view.
- Jobs example: stale after 21 days; recruiter / HR / hiring-team contact
  matches an existing job or creates a new one (no separate "lead").
- Everyone, the maintainer included, brings their own Google OAuth client
  (Desktop app, `gmail.modify`), set up through the guided `init` / UI
  wizard, and stays in Google's Testing mode (re-sign-in every 7 days). No shared
  client for now; Google verification of a shared one may come later.
- User topics live in `data/topics/` (gitignored), not the repo, so
  personal topics never end up in a commit; the repo ships only examples.
- Read the Primary inbox category by default; the user can add Updates,
  Promotions, Social and Forums in Settings (or `categories`). Spam, Sent
  and Drafts are never read. Jev is only called for new messages in the
  chosen categories. (First real run: 124 Primary vs 153 in the whole
  inbox over 14 days; the rest were Gmail-categorised as Updates etc.)
- Claude summaries are future work, after the core is locked.
- Start fresh: the jobs-only prototype was deleted rather than
  generalized.

## Open questions

- CLI name: named after the repo (`jev-gmail-filter`) for now; a shorter
  alias could be added later.
- Which candidate field kinds are needed beyond `org` / `title` (person,
  amount, date, order number)? If some are generic, they could move into
  jevfilter's extractors.
- Resolving a review in the UI should update the Gmail label.
- Poll interval: 5 minutes by default (`watch --interval`); revisit once
  it runs on real mail.

## Future: summaries with Claude

Not core. Once topics and items are stable, add summaries using the
**Claude Agent SDK**:

- **Prompt templates** stored as files in `prompts/*.md` (frontmatter +
  prompt body), created, edited and saved in the UI. Examples that ship:
  - *Daily activity* — what arrived and what changed today, per topic.
  - *Weekly review* — items that moved, went stale, or need a follow-up.
  - *Monthly overview* — trends per topic (e.g. applications sent vs
    responses).
  - *Per-topic summary* — current state of one topic's items.
- **Frontmatter** sets scope and cadence, e.g. `topics: [Jobs]`,
  `window: 7d`, `schedule: weekly`.
- **Agent, not one-shot:** Claude gets read-only tools over the local
  database (list items, list emails / results by topic and time window,
  get item history) and decides what to pull, rather than being handed a
  data dump.
- Outputs saved as digests (viewable in the UI; optionally emailed to
  self). Jev stays the classifier; Claude only reads and writes prose.
- Needs its own API key and a cost estimate per run.
