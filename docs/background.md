---
title: Background
tags: [jev-gmail-filter, jev, typesafe, gmail]
status: draft
---

# Background

## Problem

Gmail's built-in filters match on keywords, senders and headers. They
can't express *meaning*: "emails where a recruiter is reaching out about
a role", "receipts for things I'll need to expense", "anything from my
kid's school that needs a reply". People either hand-sort, or write
brittle keyword rules that miss half the cases and catch the wrong ones.

And sorting is only half of it. For a lot of topics the emails describe
something *ongoing* — a job application, an order, a support case, a
deal — that moves through stages and goes quiet when someone forgets to
follow up. Nothing in Gmail tracks that.

## Idea

An open-source, self-hosted Gmail filter where you define **topics** in
plain English. Jev judges each incoming email against them and code does
the rest: labels, grouping emails into tracked items, moving items
through stages, and flagging items that have gone stale.

## Why Jev

Each decision — "does this email belong to this topic?", "which category
is it?", "which tracked item is it about?" — is a narrow semantic
judgment. Ordinary code can't make it; a generative LLM is overkill and
harder to trust. TypeSafe's **Jev** (a System One model) returns typed
answers with calibrated probabilities, so code owns the workflow and uses
those probabilities to decide when to act and when to ask the user.
Docs: <https://docs.typesafe.ai>.

## First use case: job search

Chosen first because it exercises every feature:

- Nearly every application produces an email (LinkedIn Easy Apply,
  Indeed, and most ATSs — Greenhouse, Lever, Workday, Ashby — send
  confirmations), and everything after (interviews, rejections, offers)
  also arrives by email. The inbox alone is a near-complete record, so no
  LinkedIn/Indeed integration is needed.
- It needs topic membership, categories (stages),
  grouping by company + role, a status pipeline, and stale detection.

If the design handles jobs well, it should handle other topics — orders,
travel, bills, support tickets, school, clients — through configuration
alone.

An earlier prototype, hard-coded to jobs, validated the approach (a live Jev run on four synthetic job emails
classified all of them correctly, including rejecting a job-alert
digest).

Its judging layer — topics as files, selecting field values from
candidates, a review band, matching an email to a tracked item — turned
out to be the same layer [semantic-pubsub-jev](https://github.com/damiensmith1/semantic-pubsub-jev)
had needed. So it was pulled out into a standalone library,
[jevfilter](https://github.com/damiensmith1/jevfilter) (on PyPI). The
prototype was then discarded; this app is being rebuilt from scratch as a
generic Gmail front end on top of jevfilter, per [design](design.md).

## Prior art

- [jevfilter](https://github.com/damiensmith1/jevfilter) — the judging
  library this app is built on.
- [semantic-pubsub-jev](https://github.com/damiensmith1/semantic-pubsub-jev) — earlier project using Jev for routing
  judgments; same model and API.
- Gmail filters — keyword/header rules, no meaning, no state.
- Job trackers (Huntr, Teal, spreadsheets) — manual entry, single domain.
- LLM email assistants — generate text; not typed, not calibrated, and
  usually hosted.
