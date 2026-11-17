# RevBridge Connect — tracking milter (pixel, click, List-Unsubscribe, Message-ID/VERP).
# Speaks the native milter protocol on TCP 8901; the Postfix of the MTA connects to it.
#
# Python <= 3.12 is required: pymilter 1.0.5 (a C extension) manages the thread state of
# the libmilter C threads in a way that is incompatible with 3.13+/3.14, which fails with
# "Fatal Python error: _PyThreadState_Attach: non-NULL old thread state".
# Build context = the repository root.

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libmilter-dev libmilter1.0.1 libffi-dev libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.11.9"

WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY libs/connect-lib libs/connect-lib
COPY shared/pyproject.toml shared/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/tracking/pyproject.toml services/tracking/pyproject.toml
COPY services/milter/pyproject.toml services/milter/pyproject.toml
COPY services/infra/pyproject.toml services/infra/pyproject.toml
RUN uv sync --frozen --no-dev --package mail-milter --extra milter --no-install-workspace

COPY shared/src shared/src
COPY services/milter/src services/milter/src
RUN uv sync --frozen --no-dev --package mail-milter --extra milter

# ---- runtime ----
# libmilter1.0.1 must stay in the final image: pymilter dlopens it.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini libmilter1.0.1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /workspace /workspace
ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2
WORKDIR /workspace
USER nobody
EXPOSE 8901
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "mail_milter.tracking_milter.milter_server"]
