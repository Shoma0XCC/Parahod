# syntax=docker/dockerfile:1.7-labs
FROM python:3.12-slim AS base
WORKDIR /app

# установить uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv

# сначала зависимости
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# исходники
COPY . .

RUN mkdir -p /app/data

# важно: запускать через uv, иначе системный python не увидит .venv
CMD ["uv", "run", "python", "main.py"]
