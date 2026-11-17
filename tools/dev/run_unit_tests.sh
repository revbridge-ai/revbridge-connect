#!/usr/bin/env bash
# Unit tests inside the app image, against the current source tree (bind-mounted).
#   bash tools/dev/run_unit_tests.sh [test files...]
# Uses the minimal runner tools/dev/run_pure_tests.py (no pytest in the image). Tests that
# need pytest fixtures are reported as skipped; run them with pytest in a dev venv.
set -euo pipefail
cd "$(dirname "$0")/../.."
IMAGE="${IMAGE:-revbridge/connect-app:dev}"
if [ "$#" -eq 0 ]; then
    set -- $(ls services/api/tests/test_*.py services/infra/tests/test_*.py \
        services/tracking/tests/test_*.py services/milter/tests/test_*.py tests/test_*.py 2>/dev/null)
fi
docker run --rm --entrypoint "" \
    -v "$PWD":/repo:ro -w /repo \
    -e PYTHONPATH=/repo/services/api/src:/repo/services/tracking/src:/repo/services/milter/src:/repo/services/infra/src:/repo/shared/src:/repo/libs/connect-lib \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "$IMAGE" python tools/dev/run_pure_tests.py "$@"
