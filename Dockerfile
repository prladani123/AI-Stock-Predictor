FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2

WORKDIR /app
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY fixtures ./fixtures
COPY market_memory ./market_memory
RUN uv pip sync --system --require-hashes requirements.lock \
    && uv pip install --system setuptools==80.9.0 \
    && pip install --no-deps --no-build-isolation .

RUN useradd --create-home --uid 10001 researcher \
    && chown -R researcher:researcher /app
USER researcher

EXPOSE 8000

CMD ["python", "-m", "banknifty_foundation.bootstrap"]
