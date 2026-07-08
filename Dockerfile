# TokenOps proxy — multi-stage production image.
#
# Build:  docker build -t tokenops-proxy .
# Run:    docker run -p 8000:8000 --env-file .env tokenops-proxy

# ---------------------------------------------------------------- builder
FROM python:3.11-slim AS builder

WORKDIR /build

# Build deps for wheels that lack manylinux binaries on this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install \
       "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"

# ---------------------------------------------------------------- runtime
FROM python:3.11-slim AS runtime

# curl is required by HEALTHCHECK only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system tokenops \
    && useradd --system --gid tokenops --no-create-home tokenops

WORKDIR /app

COPY --from=builder /install /usr/local
COPY proxy/ proxy/
COPY agent/ agent/
COPY db/ db/

USER tokenops

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "proxy.main:app", "--host", "0.0.0.0", "--port", "8000"]
