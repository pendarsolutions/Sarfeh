#!/usr/bin/env bash
# Set the application version everywhere it appears.
#   scripts/bump-version.sh 0.3.0
# (install.sh has no version of its own: it installs the one in deploy/finops-agent.)
# Then add a CHANGELOG section and docs/releases/v0.3.0.md, commit, and push the tag (see docs/releasing.md).
set -euo pipefail

NEW="${1:?usage: scripts/bump-version.sh X.Y.Z}"
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a version: $NEW" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

sed -i -E "s/^VERSION=\"[^\"]*\"/VERSION=\"$NEW\"/" deploy/finops-agent
sed -i -E "0,/^version = \"[^\"]*\"/s//version = \"$NEW\"/" pyproject.toml
sed -i -E "s/(FastAPI\(title=\"Sarfeh API\", version=)\"[^\"]*\"/\1\"$NEW\"/" apps/api/finops_api/main.py
sed -i -E "s/^SARFEH_VERSION = \"[^\"]*\"/SARFEH_VERSION = \"$NEW\"/" apps/api/finops_api/config.py
# package.json and the first two "version" entries of the lock file (the root package)
sed -i -E "0,/\"version\": \"[^\"]*\"/s//\"version\": \"$NEW\"/" apps/web/package.json
sed -i -E "1,12s/\"version\": \"[^\"]*\"/\"version\": \"$NEW\"/" apps/web/package-lock.json

# the newest signed prices from Sarfeh's price service go into the release, so a new installation starts with them
"${PYTHON:-python3}" scripts/fetch_catalog.py ||
  echo "warning: no new catalog; the release carries the one already in data/feed (if any)" >&2

grep -n "$NEW" deploy/finops-agent pyproject.toml apps/api/finops_api/main.py apps/api/finops_api/config.py apps/web/package.json
head -12 apps/web/package-lock.json | grep -n "$NEW"
