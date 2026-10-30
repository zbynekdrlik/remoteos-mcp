#!/usr/bin/env bash
# Create GitHub release v<pyproject version> on this commit (#63). CI runs it on every merge to main.
# Idempotent: an existing release is left exactly as it is, so a re-run never un-holds one (a hold is
# the `prerelease` mark set in the GitHub UI). Its publish date is the release's age for the
# stable/canary resolver (src/remoteos/release_channel.py).
set -euo pipefail

: "${GITHUB_SHA:?GITHUB_SHA is not set}"
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
if [ -z "$version" ]; then
  echo "no version line in pyproject.toml" >&2
  exit 1
fi
tag="v$version"
if gh release view "$tag" >/dev/null 2>&1; then
  echo "release $tag already exists, nothing to do"
  exit 0
fi
gh release create "$tag" --target "$GITHUB_SHA" --title "$tag" --generate-notes
echo "release $tag created on $GITHUB_SHA"
