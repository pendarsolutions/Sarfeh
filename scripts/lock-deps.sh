#!/usr/bin/env bash
# Refresh constraints.txt: resolve requirements.txt from scratch in the app image's Python and pin everything it
# installs. Run it after changing requirements.txt, then run the tests and commit both files.
#   scripts/lock-deps.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_IMAGE="$(sed -n 's/^FROM \(python:[^ ]*\).*/\1/p' "$ROOT/Dockerfile" | head -1)"
HEADER="$(sed -n '/^#/p' "$ROOT/constraints.txt")"
pins="$(docker run --rm -i "$PY_IMAGE" sh -c 'cat > /tmp/r.txt && pip install -q -r /tmp/r.txt >/dev/null && pip freeze' \
  < "$ROOT/requirements.txt" | sort -f)"
printf '%s\n%s\n' "$HEADER" "$pins" > "$ROOT/constraints.txt"
echo "constraints.txt: $(printf '%s\n' "$pins" | grep -c '==') packages, from $PY_IMAGE"
