# jev-gmail-filter

[![CI](https://github.com/damiensmith1/jev-gmail-filter/actions/workflows/ci.yml/badge.svg)](https://github.com/damiensmith1/jev-gmail-filter/actions/workflows/ci.yml)

Filter Gmail with plain-English topics. Describe what you care about
("receipts for things I bought", "recruiters contacting me about a role")
and the app labels matching emails, groups them into tracked items and
flags anything that has gone quiet. Judging is done by
[jevfilter](https://github.com/damiensmith1/jevfilter) with TypeSafe's Jev.

Self-hosted and local: your email and data stay on your machine.

> Early development: not usable yet. See [docs/](docs/) for the design.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
```

Example topics live in [`topics/examples/`](topics/examples/).

## License

MIT
