FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY collector ./collector

RUN pip install --upgrade pip && pip install .

RUN useradd --system --uid 10001 --no-create-home statless \
    && mkdir -p /app/data && chown -R statless:statless /app
USER statless

# GeoIP2 DB + SQLite file live here (mount a volume in production)
VOLUME /app/data
EXPOSE 8000

# Use the `statless` entrypoint (not bare uvicorn) so STATLESS_HOST/STATLESS_PORT apply.
CMD ["statless"]
