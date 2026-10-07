#!/usr/bin/env bash
#
# update-lockfiles.sh — regenerate requirements.lock / requirements-aws.lock.
#
# The Docker image installs ONLY from these hash-pinned lockfiles (never fresh
# resolution), so a dependency moves when — and only when — you rerun this,
# run the tests against the result, and commit it. Needs `uv`
# (https://docs.astral.sh/uv/; `pipx install uv` or `pip install uv`).
#
#   ./contrib/scripts/update-lockfiles.sh            # newest versions allowed by pyproject.toml
#   ./contrib/scripts/update-lockfiles.sh -P fastapi # upgrade just one package
#
# Resolution targets the image's Python (3.12) for every platform. setuptools
# is included because the Dockerfile builds the portal package without build
# isolation, so the build backend is pinned too.
set -euo pipefail
cd "$(dirname "$0")/../.."

common=(--generate-hashes --universal --python-version 3.12 --no-header)
build_reqs="setuptools>=70.1"

echo "$build_reqs" | uv pip compile pyproject.toml - --extra mcp "${common[@]}" -o requirements.lock "$@"
echo "$build_reqs" | uv pip compile pyproject.toml - --extra mcp --extra aws "${common[@]}" -o requirements-aws.lock "$@"
echo "Updated requirements.lock and requirements-aws.lock — run the tests before committing."
