FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY app app
COPY alembic.ini .
COPY migrations migrations
COPY scripts scripts
RUN useradd --system --uid 10001 shygys
USER 10001
ENV PATH="/service/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
