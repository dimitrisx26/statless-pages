FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv (single static binary)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependency layer: only pyproject + lock invalidate it
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY collector ./collector
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home statless \
    && mkdir -p /app/data && chown -R statless:statless /app
USER statless

# GeoIP2 DB + SQLite file live here (mount a volume in production)
VOLUME /app/data
EXPOSE 8000

# Use the `statless` entrypoint (not bare uvicorn) so STATLESS_HOST/STATLESS_PORT apply.
# Run inside the uv-managed venv so uvicorn & deps resolve.
CMD ["uv", "run", "--no-sync", "statless"]
