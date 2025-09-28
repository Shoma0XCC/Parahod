# syntax=docker/dockerfile:1.7-labs
FROM ghcr.io/astral-sh/uv:0.4.24-python3.12-slim
WORKDIR /app

# зависимости
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# исходники
COPY . .
RUN mkdir -p /app/data

# Render прокидывает порт в $PORT — используем его
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"]

