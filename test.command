#!/bin/bash
# Run the attendance test suite in the same Python image the app ships on (python:3.12-slim),
# so it works regardless of the Mac's own Python. Extra args go to pytest, e.g. ./test.command -k roster
cd "$(dirname "$0")/attendance" || exit 1
exec docker run --rm -v "$PWD":/app -w /app python:3.12-slim sh -c \
  "pip install -q --disable-pip-version-check --root-user-action=ignore -r requirements.txt -r requirements-dev.txt \
   && python -m pytest -p no:cacheprovider $*"
