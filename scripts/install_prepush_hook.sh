#!/bin/sh
# Point git at the versioned hook directory so the pre-push gate travels with
# the repository instead of living in .git/hooks (which is never cloned).
#
#   sh scripts/install_prepush_hook.sh
#   git config --unset core.hooksPath      # undo
#
# The hook itself is .githooks/pre-push; see docs/pre-push-gate.md.

set -e

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "install_prepush_hook: not inside a git repository" >&2
  exit 1
}

chmod +x "$repo_root/.githooks/pre-push" 2>/dev/null || true
chmod +x "$repo_root/scripts/prepush_gate.py" 2>/dev/null || true

git -C "$repo_root" config core.hooksPath .githooks

echo "core.hooksPath = $(git -C "$repo_root" config --get core.hooksPath)"
echo "hook          = $repo_root/.githooks/pre-push"
echo "rules         = $repo_root/scripts/prepush_rules.toml"
echo "private terms = ${THREADWEAVE_PREPUSH_PRIVATE:-~/.threadweave/prepush-private.txt}"
