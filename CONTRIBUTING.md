# Contributing to ThreadWeave

Thanks for looking. This is a working system with a small maintainer
team, so the fastest path to a merged change is: open an issue first for
anything larger than a bug fix, keep the change focused, and bring
evidence.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/PowerLooming/ThreadWeave
cd ThreadWeave
bash setup.sh
uv run threadweave demo --serve   # a populated palace, no tenant required
```

The demo palace is fictional and isolated (`~/.threadweave/demo.sqlite3`).
It is also the fastest way to see the capture, search and answer loop
while you work on the code.

## Running the tests

```bash
uv run pytest -q                       # the full suite
uv run pytest tests/test_demo.py -q    # one file while iterating
```

The suite is the review evidence. If you change behaviour, add the test
that would have caught the bug, and put the real counts in your pull
request. `tests/conftest.py` redirects the audit log, the entry store and
the notification queue into a temp directory, so tests never touch a
running installation's data. Keep it that way: a test that writes to
`~/.threadweave` is a bug.

## What a good change looks like

- One concern per pull request, with the reasoning in the body: why, what
  changed, how you verified it, and any breaking change.
- Type hints, docstrings that explain the decision rather than restating
  the code, and comments that name the incident or measurement that made
  the code necessary.
- Plain prose in commit messages and pull request bodies. No emoji
  narration, no adjectives standing in for a measurement.
- Fail closed. Anything security-shaped (a gate, an ACL, a sensitivity
  level, a token) must deny on the unexpected input, and must say so in
  the log rather than raising into a request path.

## Version and changelog

Four places carry the version and they must agree: `pyproject.toml`,
both literals in `src/threadweave/api.py`, the assertion in
`tests/test_api.py`, and a `## [X.Y.Z]` section in `CHANGELOG.md`. You
normally do not bump anything: the release workflow bumps the patch
version, syncs all four, tags and publishes on every push to `master`.
Write your user-visible change under `## [Unreleased]` in
`CHANGELOG.md`, because that section becomes the release announcement
verbatim. A push that only touches docs skips the bump.

Because every merge to master is a release, say in your pull request
whether you expect one.

## Public repository hygiene

This repository is public and 1:1 with what the maintainer runs. Before
you push, check that your change adds no real tenant, customer, person,
hostname, token or internal document. Illustrative names belong in
examples; invented ones are always fine. Run the gate:

```bash
uv run python scripts/prepush_gate.py     # what the git hook runs
```

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

Contributions are accepted under the MIT license (see [LICENSE](LICENSE)).
