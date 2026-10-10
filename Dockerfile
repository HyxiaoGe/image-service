FROM python:3.11-slim

WORKDIR /app

# Install dependencies from uv.lock so the image matches what tests ran against.
# uv comes from PyPI (same network path as before) rather than a ghcr.io image.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv==0.11.16

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-cache

COPY src/ src/

RUN mkdir -p storage/images

EXPOSE 8090

CMD ["uvicorn", "src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090"]
