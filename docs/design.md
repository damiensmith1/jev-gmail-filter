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
   - plus values already seen on that topic's items

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
2. Walk through creating a Google OAuth Desktop client; open the browser
   to authorise; save the token.
3. Pick starter topics: bundled examples (Jobs, Receipts, Travel, …) or
   blank.
4. **Choose the backscan window** (e.g. 1 day / 1 week / 2 weeks / 1 month
   / custom), with an estimated email count and Jev cost.
5. Dry run option: classify without writing Gmail labels, review results,
   then enable labels.

## Code layout (target)

Package `jev_gmail_filter` (CLI `jev-gmail-filter`): `gmail.py` (OAuth, polling, labels),
`candidates.py` (extraction by field kind), `pipeline.py` (candidates →
jevfilter → items → labels), `db.py`, `cli.py`, `app.py` (Streamlit).
`topics/examples/` ships sample topics.

Built from scratch; nothing carries over from the discarded prototype.

## Depends on jevfilter

Built in jevfilter 0.1.0: topics, `Filter.judge`, `explain`, thresholds
and reasons, failure policy, serialisable results.

Needed from jevfilter before this app can run end to end:

| Need | jevfilter feature | Status |
|------|-------------------|--------|
| Match an email to an existing item | `match_item` | not built |
| Status pipeline, stale detection | `track.next_status`, `track.is_stale` | not built |
| Fast backscans | `AsyncFilter.judge_many` | not built |
| Spend cap on backscans | `Budget` | not built |
| Many topics per email without hitting limits | request packing / splitting | not built |

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
- Web UI: Streamlit (fastest for a local tool; the UI only reads/writes
  SQLite and topic files, so swapping later is cheap).
- Jobs example: stale after 21 days; recruiter / HR / hiring-team contact
  matches an existing job or creates a new one (no separate "lead").
- Scan the Primary inbox only — skip Promotions, Social, Updates,
  Forums, Spam, Sent, Drafts. Jev is only called for new messages there.
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
- Poll interval default (proposed: 5 minutes).

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
