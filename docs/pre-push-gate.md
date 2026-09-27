# Pre-push hygiene gate

Nothing leaves for a public remote by accident. The gate runs as a `pre-push`
hook and looks at exactly what the push would make public: the commits in the
range, the lines they add, and, when the branch is new to that remote, the whole
tree that is about to become visible.

Install it once per clone:

```bash
sh scripts/install_prepush_hook.sh
```

That sets `core.hooksPath` to the versioned `.githooks` directory, so the hook
travels with the repository instead of living in `.git/hooks`, which is never
cloned. Undo with `git config --unset core.hooksPath`.

Manual runs:

```bash
python scripts/prepush_gate.py --all                  # everything not on origin
python scripts/prepush_gate.py --all --advisory       # plus the encoder pass
python scripts/prepush_gate.py --all --json           # machine readable
python scripts/prepush_gate.py --list-rules           # what is actually loaded
```

Exit codes: `0` clean or warnings only, `1` blocking findings, `2` the gate could
not run. The hook fails open on `2`, loudly, because a broken seatbelt must not
stop work.

## Two layers, on purpose

The blocking layer is deterministic: regexes and filename rules. A leaked tenant
id, API key or absolute home path is an exact string, and a regex cannot miss it
once it is written. A classifier can only ever answer "0.83 probably", and a
secret scan that misses one real identifier is worse than no scan. This is the
same conclusion the typed decision layer reached from the other side: for
anything deterministic, do not ask a model. `language_id.py` exists for exactly
that reason, and this gate follows the rule.

The advisory layer (`--advisory`) is the opposite: the genuinely fuzzy questions
where a regex is the wrong tool. Does this commit message or comment name an
organisation, reference an internal document, or read as internal commentary?
That is a judgment, so it is asked of the local NLI encoder from the typed
decision layer, one pass per state, and it never blocks a push. It is opt-in
because it has to load model weights, which a hook should not do on every push.

## Rules

`scripts/prepush_rules.toml` holds shapes only and is published, so it must never
contain a real identifier. Site-specific terms live in a private overlay that is
never committed:

```
~/.threadweave/prepush-private.txt        # default
$THREADWEAVE_PREPUSH_PRIVATE              # or point it somewhere else
./.prepush-private.txt                    # or a gitignored file in the repo
```

Overlay format, one term per line:

```
# comments are ignored
[block]                 # terms below stop a push
nordvik
<tenant>.onmicrosoft.com
re:tenant[0-9]{6}       # re: prefix for a regex instead of a literal
[warn]                  # terms below only print
internal-notes
```

Literals are matched case-insensitively as plain text, so no escaping is needed.
The overlay is a good home for tenant aliases, customer and employer names,
colleague names, internal host names and internal document folder names.

Rule fields in the TOML:

| Field | Meaning |
|---|---|
| `id` | short name that shows up in the report |
| `pattern` | regex, case-insensitive, matched per line (or per path) |
| `scope` | `content` (added lines), `commit` (messages and author), `path`, `any` |
| `severity` | `block` stops the push, `warn` only prints |
| `why` | the sentence printed with the finding, so the fix is obvious |

Suppression, in order of preference:

1. Fix the line, or do not push it.
2. `[allow] patterns` in the rules file for a whole class of matches (published
   placeholders, `.env.example`, `noreply@github.com`).
3. `prepush-allow` anywhere on the line for a single deliberate exception.
4. A file that is itself a fixture set can exempt itself with a comment line of
   its own: `prepush-allow-file: *` for every rule in the published rules file,
   or `prepush-allow-file: tenant-domain, openai-key` for a named list. Terms
   from the private overlay are never exempt, so a real tenant alias pasted into
   a fixture file still stops the push. The rule ids in the marker must be
   exactly right: an unknown id exempts nothing, and the marker has to be the
   whole comment line, so prose about the marker does not silently disarm the
   file that documents it.
5. `git push --no-verify` for one push, which bypasses everything.

## What it scans

- Existing branch: `git diff <remote-sha> <head>` added lines, so an untouched
  line in history is not re-reported every time you push.
- New branch on that remote (all-zero remote sha): the full tree of the pushed
  commit, because that is the moment the whole content becomes public.
- Every commit message and author line in the range.
- Every commit being pushed, even after a rebase: the base is resolved from the
  remote sha when it is an ancestor and from the pushed commits otherwise.

Generated data is skipped for content (`uv.lock`, `*.min.js`, images, `dist/`),
but path rules still apply to those files.

## Matching output and privacy

Short matches (24 characters or fewer) are printed as they are: a tenant alias or
mail address is only actionable if you can see which one it is, and the output
goes to the machine of the person who wrote the line. Longer matches are the ones
that must not be echoed (keys, tokens, PEM blocks, connection strings), because
they end up in screenshots and pasted logs, so they print as a prefix plus their
length. `--show-matches` turns that off when you need the literal string.

## Limits, stated plainly

- A `pre-push` hook is local and bypassable. It is a seatbelt, not a control.
  The control is whatever reviews the diff before it is published.
- Regexes catch shapes and names, not meaning. A short password typed into a
  config file is only caught if it matches a shape rule or the overlay.
- Binary additions are not read (only their path is judged).
- Advisory findings are a second opinion from a model whose probabilities are
  uncalibrated, so treat the printed number as a ranking, not a decision.
