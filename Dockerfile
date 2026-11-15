FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv==0.13.8
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH=amd64
ARG TRIVY_VERSION=0.76.2
ARG GITLEAKS_VERSION=8.30.1
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) trivy_arch=64bit; gitleaks_arch=x64 ;; \
      arm64) trivy_arch=ARM64; gitleaks_arch=arm64 ;; \
      *) exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${trivy_arch}.tar.gz" \
      | tar -xz -C /usr/local/bin trivy; \
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gitleaks_arch}.tar.gz" \
      | tar -xz -C /usr/local/bin gitleaks

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
RUN chmod +x /app/scripts/git_askpass.py
RUN uv sync --locked --no-dev --extra code-agent --extra security-agent --no-editable

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/agents-cache
RUN useradd --system --uid 10001 --home-dir /app agents
USER agents

CMD ["uvicorn", "agents_platform.api:app", "--host", "0.0.0.0", "--port", "8000"]
