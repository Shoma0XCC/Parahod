# syntax=docker/dockerfile:1.7-labs
FROM python:3.12-slim

WORKDIR /app
ENV UV_LINK_MODE=copy

# 1) Устанавливаем uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv

# 2) Ставим зависимости по lock'у (лучший кэш)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# 3) Копируем код и создаём каталог для данных
COPY . .
RUN mkdir -p /app/data

# 4) Запуск как ВЕБ-СЕРВЕР (Render Web Service прокинет $PORT)
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"]
