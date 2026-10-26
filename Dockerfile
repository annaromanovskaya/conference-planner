FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
COPY README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN groupadd --system conference-planner && \
    useradd --system --gid conference-planner conference-planner && \
    mkdir -p /data && \
    chown conference-planner:conference-planner /data
COPY --from=builder /install /usr/local
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
USER conference-planner
WORKDIR /app
ENTRYPOINT ["conference-planner"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 CMD ["conference-planner-healthcheck"]
