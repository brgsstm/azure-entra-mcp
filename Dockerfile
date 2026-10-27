FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /bin/uv
# Install exactly what uv.lock pins (--frozen), into /opt/venv, using the image's Python.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /src
# Dependencies first so this layer is reused when only src/ changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8000
RUN useradd --uid 10001 --create-home app
COPY --from=build /opt/venv /opt/venv
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz')"
CMD ["azure-entra-mcp"]
