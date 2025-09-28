# syntax=docker/dockerfile:1.7-labs
FROM python:3.12-slim
WORKDIR /app
ENV UV_LINK_MODE=copy

# install uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project

COPY . .
RUN mkdir -p /app/data

# <<< ключевая строка: используем shell-форму, PORT подставится Render-ом >>>
CMD sh -c 'uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}'
