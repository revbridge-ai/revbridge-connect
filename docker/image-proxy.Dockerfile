# RevBridge Connect — image proxy.
# Puts a solid background behind transparent PNGs (no black logo in Gmail/Outlook) and converts SVG.
# Build context = the repository root.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tini libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# requirements.lock = exact versions (pip freeze of the validated image); requirements.txt = version ranges
COPY services/image-proxy/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY services/image-proxy/main.py /app/src/main.py
RUN touch /app/src/__init__.py

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER nobody
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
