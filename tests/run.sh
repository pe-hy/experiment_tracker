#!/usr/bin/env bash
# Build the fixture site, then render every view under node and assert on the result.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
NODE="${NODE:-node}"
if ! command -v "$NODE" >/dev/null 2>&1; then
  echo "node not found. Set NODE=/path/to/node (VS Code Server ships one)." >&2
  exit 2
fi

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

echo "== reindex fixture =="
"$PY" scripts/reindex.py --data tests/fixture --out "$OUT/data"

echo "== python self-checks =="
"$PY" tests/track_test.py

echo "== render tests =="
"$NODE" --check site/assets/app.js 2>/dev/null || {
  cp site/assets/app.js "$OUT/app.mjs"; "$NODE" --check "$OUT/app.mjs"; }
"$NODE" tests/render_test.mjs "$OUT"

# Fixtures prove the views; the click-through proves them on the data we actually
# serve, driving folds, toggles and full-text rendering like a reader would.
if ls data/projects/*/runs/*.json >/dev/null 2>&1; then
  echo "== real-data click-through =="
  REAL="$(mktemp -d)"
  trap 'rm -rf "$OUT" "$REAL"' EXIT
  "$PY" scripts/reindex.py --data data --out "$REAL/data"
  "$NODE" tests/clickthrough_real.mjs "$REAL"
else
  echo "== real-data click-through skipped (no tracked projects) =="
fi
