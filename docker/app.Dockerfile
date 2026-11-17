# RevBridge Connect — one image for the Python services:
#   api, queue-processor, ledger-reaper, webhook-dispatcher, tracking, events and the
#   scheduler of the deliverability jobs.
# Each compose container picks its command. The milter has an image of its own
# (pymilter needs Python <= 3.12, see docker/milter.Dockerfile).
# Build context = the repository root.

FROM python:3.14-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir "uv==0.11.9"

WORKDIR /workspace

# Manifests first: caches the dependency resolution.
COPY pyproject.toml uv.lock ./
COPY libs/connect-lib libs/connect-lib
COPY shared/pyproject.toml shared/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/tracking/pyproject.toml services/tracking/pyproject.toml
COPY services/milter/pyproject.toml services/milter/pyproject.toml
COPY services/infra/pyproject.toml services/infra/pyproject.toml
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace

COPY shared/src shared/src
COPY services/api/src services/api/src
COPY services/tracking/src services/tracking/src
COPY services/milter/src services/milter/src
COPY services/infra/src services/infra/src
RUN uv sync --frozen --no-dev --all-packages

# ---- runtime ----
FROM python:3.14-slim

# dnsutils: DNS/DNSBL checks of the deliverability jobs
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini ca-certificates dnsutils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /workspace /workspace
# Deliverability jobs (read by the scheduler) and the installation scripts
COPY services/infra/scripts /workspace/services/infra/scripts
COPY scripts /workspace/scripts
COPY connect.toml /workspace/connect.toml

ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /workspace
USER nobody
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "mail_api.app.api.app:app", "--host", "0.0.0.0", "--port", "8001"]
