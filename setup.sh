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
    if command -v "$candidate" &> /dev/null; then
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
    echo "Python not found. Install Python 3.11+ and try again."
    exit 1
fi
echo "Python: $($PY --version 2>&1)"

# ---- Check/install uv ----
if ! command -v uv &> /dev/null; then
    echo "→ Installing uv..."
    $PY -m pip install uv
fi
echo "✅ uv: $(uv --version)"

# ---- Create venv ----
echo ""
echo "→ Creating virtual environment..."
uv venv --python 3.11 .venv
source .venv/Scripts/activate  # Windows
# source .venv/bin/activate    # macOS/Linux

# ---- Install ThreadWeave + deps ----
echo ""
echo "→ Installing ThreadWeave and dependencies..."
uv pip install -e ".[dev]"

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
echo "  Start the server:  threadweave serve"
echo "  Or:                python -m uvicorn threadweave.api:app --reload"
echo ""
echo "  Quick test:"
echo "    threadweave detect 'We should use Redis for caching'"
echo "    threadweave save --wing engineering --room caching --content 'Always use Redis Cluster in production'"
echo "    threadweave search 'Redis caching'"
echo "========================================"
