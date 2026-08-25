#!/usr/bin/env bash
# Update this checkout in place: pull latest main, reinstall, rerun the
# test suite as the final gate. There's no server or fleet to deploy to --
# this tool runs on your own machine, so "deployment" here just means
# keeping the local install current and proven-working after doing so.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "error: uncommitted changes present -- commit or stash them first" >&2
    exit 1
fi

before=$(git rev-parse HEAD)
echo "== pulling latest =="
# --ff-only, not a merge: fails loudly on any real divergence instead of
# quietly creating a merge commit in a checkout meant to just track main.
git pull --ff-only origin main
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    echo "already up to date"
else
    echo "== changes =="
    # The record of what changed and why now lives in git log / GitHub's
    # own commit and compare views, not a CHANGELOG.md.
    git log --oneline "$before".."$after"
fi

if [ ! -f .venv/bin/activate ]; then
    echo "error: .venv not found -- run this from the harness checkout root" >&2
    exit 1
fi

echo "== reinstalling =="
.venv/bin/pip install -r requirements-lock.txt -q
.venv/bin/pip install -e . --no-deps -q

echo "== running tests =="
# .venv/bin/python3 explicitly, not a bare `python3` or an assumed-active
# shell -- bare python3 has none of this project's dependencies installed.
.venv/bin/python3 -m pytest tests/ -q

echo "== update complete =="
