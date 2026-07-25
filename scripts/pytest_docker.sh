#!/bin/sh
# Run the Python test suite in a container with the production dependency set.
#
# Why: the pinned runtime deps target Python 3.12; a developer host on a newer
# interpreter cannot build pydantic-core/psycopg wheels. This runs the suite in
# an image derived from the built prediction-service image (identical library
# versions to production) with the dev test deps layered on top.
#
# Usage (from the repo root):
#   sh scripts/pytest_docker.sh                 # whole suite
#   sh scripts/pytest_docker.sh tests/test_models.py -q
set -eu

IMAGE=tala-pala-pytest:latest
BASE=tala-pala-prediction-service:latest

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker image inspect "$BASE" >/dev/null 2>&1 || {
    echo "base image $BASE missing — run: docker compose build prediction-service" >&2
    exit 1
  }
  printf 'FROM %s\nUSER root\nCOPY prediction-python/requirements-dev.txt /tmp/requirements-dev.txt\nRUN pip install --no-cache-dir -q -r /tmp/requirements-dev.txt\n' "$BASE" \
    | docker build -q -t "$IMAGE" -f - . >/dev/null
fi

exec docker run --rm \
  -v "$(pwd)/prediction-python:/src" \
  -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPYCACHEPREFIX=/tmp/pycache \
  -e PYTEST_ADDOPTS="-p no:cacheprovider" \
  "$IMAGE" python -m pytest "$@"
