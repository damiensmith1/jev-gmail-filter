---
title: Requirements
tags: [jev-gmail-filter, requirements]
status: draft
---

# Requirements

## Core concepts

- **Topic** — a user-defined filter: a name and a plain-English
  description of what belongs, in the
  [jevfilter topic format](https://github.com/damiensmith1/jevfilter/blob/main/docs/topic-format.md).
  Optionally:
  - **categories** — one is picked per matching email (e.g. for jobs:
    applied, recruiter, interview, rejection, offer)
  - **fields** — values picked from the email (e.g. company, role)
  - **flags** and **scores** — extra yes/no conditions (e.g. needs a
    reply) and ratings (e.g. urgency)
  - **tracking** — group matching emails into **items** by fields
    (e.g. company), with a status pipeline driven by categories and a
    stale-after-N-days rule
- **Item** — one tracked thing inside a topic (a job application, an
  order, a support case). Has fields, a status, and its linked emails.

## Functional

- **F1 — First-run setup.** A guided flow that: walks the user through
  creating **their own Google OAuth client** step by step and connects
  Gmail with it, asks for the Jev API key, lets the user **choose the
  backscan window** (how far back to scan), and lets them start from
  example topics or a blank one.
- **F2 — Define topics easily.** A minimal topic is just a name and a
  description. Categories and tracking are optional additions. Topics can
  be created and edited in the web UI and are stored as plain files, so
  they can also be edited by hand, shared, and version-controlled.
- **F3 — Classify.** For each new email, Jev decides which topics it
  belongs to (an email may match several, or none), and for each matching
  topic, its category, fields, flags and scores.
- **F4 — Track.** For tracked topics: match the email to an existing item
  or create a new one; update its fields and status; keep the email
  linked. For jobs this includes recruiter / HR / hiring-team contact.
- **F5 — Label.** Write Gmail labels back (`<Topic>` and
  `<Topic>/<Category>`) so results are visible in Gmail. Optional per
  topic.
- **F6 — Stale / follow-up.** Flag items with no email for the topic's
  stale window (jobs: 21 days).
- **F7 — Review queue.** Low-confidence judgments go to the user instead
  of being silently applied. Resolving one records the user's decision.
- **F8 — Manual items.** Add or edit an item by hand.
- **F9 — View.** Local web UI: per-topic lists and item pipelines, the
  review queue, recent emails, and topic editing.
- **F10 — Rescan.** Re-run a topic over a chosen window after creating or
  editing it (with a cost estimate first).
- **F11 — Choose what to read.** Pick which Gmail inbox categories are
  read (Primary by default; Updates, Promotions, Social, Forums optional).

## Non-functional

- **Open source, self-hosted.** Runs locally; no hosted backend. Setup is
  documented and short enough for a developer to finish in ~15 minutes.
- Python. Data in local SQLite.
- **Built on [jevfilter](https://github.com/damiensmith1/jevfilter).** All
  judging (questions, thresholds, item matching, tracking rules, spend
  caps) goes through the library; the app never calls Jev directly.
- **Everyone brings their own Google OAuth client**, the maintainer
  included. The project ships no shared client, so there is no app for
  Google to verify and no one else's credentials are involved.
- Secrets (`TYPESAFE_API_KEY`, Gmail OAuth client and token) stay local
  and gitignored; never logged.
- **Cost-aware.** Each email is judged once per topic version. Show
  estimated Jev spend before backscans and rescans, and cap backscan spend.
- Full jevfilter results (probabilities, reasons, topic and wording
  versions) are stored so thresholds can be retuned without re-calling
  Jev.
- Topics and thresholds are configuration, not code. Adding a topic never
  requires a code change.

## Non-goals (core)

- Acting on emails beyond labelling (no replying, archiving, deleting).
- Auto-applying to jobs or any other domain-specific automation.
- Inboxes other than Gmail (for now).
- Hosted / multi-tenant deployment.
- Gmail add-on or browser extension.

## Future (not core)

- **Summaries with the Claude Agent SDK** — see [design](design.md#future-summaries-with-claude).
- **A shared, Google-verified OAuth client** for one-click sign-in — see
  [design](design.md#google-access). Not worth it right now.
- Drafting replies or applications.
- Other mail providers (IMAP).
