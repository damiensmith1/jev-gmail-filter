# Contributing

Thanks for helping. Changes come in through pull requests from forks:

1. Fork the repo and create a branch in your fork.
2. Make your change and add tests.
3. Open a pull request against `main`.

## Rules on this repository

- `main` can only change through a pull request. Force-pushes and deletion
  are blocked.
- Every PR needs an approving review from the code owner
  ([@damiensmith1](https://github.com/damiensmith1)) and all review threads
  resolved, and passing CI (lint, tests). A new push after approval
  needs a fresh approval.
- Only the maintainer can create branches or tags in this repository.
- CI on PRs from outside contributors runs only after the maintainer
  approves it. Workflows get a read-only token.

## Keep it clean

This is a public repo for an app that reads Gmail. Never commit:

- API keys, OAuth client files (`credentials.json`), tokens (`token.json`)
  or `.env`
- real emails, names, addresses or other personal data (use made-up
  examples with `.example` domains)
- local paths, screenshots of your inbox, or database files

Tests must never call the real Jev or Gmail APIs.

Please report security issues privately; see [SECURITY.md](SECURITY.md).
