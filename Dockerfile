FROM python:3.12-slim
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-editable
COPY . .
# создаём каталог для сохранения расписаний
RUN mkdir -p /app/data
CMD ["python", "main.py"]
