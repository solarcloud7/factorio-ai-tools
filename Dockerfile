FROM python:3.11-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# .git isn't in the build context, so setuptools-scm can't derive a version from git history
# (it fails the editable build of this package). Pin a placeholder — the version is irrelevant
# at runtime. (Alternative: COPY the .git dir before this step to keep the real version.)
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

RUN uv sync --no-dev --frozen --no-cache

ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/cache/huggingface
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

ENTRYPOINT ["factorio-ai-tools", "--sse", "--port", "8000"]
