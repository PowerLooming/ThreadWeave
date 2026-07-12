#!/usr/bin/env bash
# ThreadWeave laptop setup — one command to get running.
# Run from the threadweave project root.
set -e

echo "========================================"
echo "  ThreadWeave Setup"
echo "========================================"
echo ""

# ---- Check prerequisites ----
if ! command -v uv &> /dev/null; then
    echo "❌ uv is not installed."
    echo "   Install it: https://docs.astral.sh/uv/getting-started/installation/"
    echo "   Or: pip install uv"
    exit 1
fi
echo "✅ uv found: $(uv --version)"

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
print(f'  Detector: ✅ (type={result.content_type.value}, confidence={result.confidence})')

try:
    import mempalace
    print(f'  MemPalace: ✅ (v{mempalace.__version__})')
except ImportError:
    print('  MemPalace: ⚠️ not installed (hybrid search disabled, keyword fallback works)')
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
