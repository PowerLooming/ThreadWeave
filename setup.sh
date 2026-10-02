#!/usr/bin/env bash
# ThreadWeave laptop setup — one command to get running.
# Run from the threadweave project root.
set -e

echo "========================================"
echo "  ThreadWeave Setup"
echo "========================================"
echo ""

# ---- Check/install Python ----
PY=""
for candidate in python3 python python.exe; do
    # The Windows Store ships an alias stub that answers `command -v` and
    # then refuses to run, so ask each candidate to actually start.
    if command -v "$candidate" &> /dev/null \
        && "$candidate" -c 'import sys' &> /dev/null; then
        PY="$candidate"
        break
    fi
done
# WSL fallback: try to find Windows Python from /mnt/c
if [ -z "$PY" ] && [ -f "/mnt/c/Users/$USER/AppData/Local/Programs/Python/Python313/python.exe" ]; then
    PY="/mnt/c/Users/$USER/AppData/Local/Programs/Python/Python313/python.exe"
fi
if [ -z "$PY" ] && [ -f "/mnt/c/Users/$USER/AppData/Local/Programs/Python/Python311/python.exe" ]; then
    PY="/mnt/c/Users/$USER/AppData/Local/Programs/Python/Python311/python.exe"
fi
if [ -z "$PY" ]; then
    # Not fatal: uv installs and manages its own interpreter, so a machine
    # with only uv (or only the Windows Store stub) can still set up.
    echo "No usable system Python found; uv will provide Python 3.11."
else
    echo "Python: $($PY --version 2>&1)"
fi

# ---- Check/install uv ----
if ! command -v uv &> /dev/null; then
    echo "→ Installing uv..."
    if [ -n "$PY" ]; then
        $PY -m pip install uv
    else
        echo "uv is required. Install it first:"
        echo "  https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
fi
echo "✅ uv: $(uv --version)"

# ---- Create venv ----
echo ""
if [ -x ".venv/Scripts/python.exe" ] || [ -x ".venv/bin/python" ]; then
    # Re-running setup.sh must not abort: `uv venv` refuses to replace an
    # existing environment, and a stale .venv is the common case on a
    # second run. Reuse it and let the install step refresh the packages.
    echo "→ Reusing the virtual environment in .venv"
else
    echo "→ Creating virtual environment..."
    uv venv --python 3.11 .venv
fi
source .venv/Scripts/activate  # Windows
# source .venv/bin/activate    # macOS/Linux

# ---- Install ThreadWeave + deps ----
echo ""
echo "→ Installing ThreadWeave and dependencies..."
# The connector extras are included deliberately: the verification step below
# runs the full suite, and several modules (msal for the mail connectors, the
# MCP SDK) are import-level dependencies of it. With only [dev] the suite
# aborts during collection and the first thing a new user sees is a wall of
# ModuleNotFoundError, which reads as a broken project.
uv pip install -e ".[dev,all-connectors,mcp]"

# ---- Verify ----
echo ""
echo "→ Verifying installation..."
python -c "
from threadweave.detector import detect, is_worth_saving
should, result = is_worth_saving('We decided to use PostgreSQL for the auth service.')
print(f'  Detector: OK (type={result.content_type.value}, confidence={result.confidence})')

try:
    import mempalace
    print(f'  MemPalace: OK (v{mempalace.__version__})')
except ImportError:
    print('  MemPalace: not installed (hybrid search disabled, keyword fallback works)')
"

# ---- Run tests ----
echo ""
echo "→ Running tests..."
python -m pytest tests/ -q

echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  Try the demo palace (no tenant, no credentials):"
echo "    threadweave demo --serve       # → http://127.0.0.1:8000/"
echo ""
echo "  Start the server:  threadweave serve"
echo "  Or:                python -m uvicorn threadweave.api:app --reload"
echo ""
echo "  Quick test:"
echo "    threadweave detect 'We should use Redis for caching'"
echo "    threadweave save --wing engineering --room caching --content 'Always use Redis Cluster in production'"
echo "    threadweave search 'Redis caching'"
echo "========================================"
