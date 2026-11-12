FROM python:3.12-slim

# Unprivileged user; the image never runs as root.
RUN useradd --create-home --uid 10001 rugguard
WORKDIR /app
COPY pyproject.toml README.md ./
COPY rugguard ./rugguard
RUN pip install --no-cache-dir ".[api]" && rm -rf /root/.cache
COPY seeds ./seeds
COPY demo ./demo
RUN mkdir -p /app/data && chown rugguard:rugguard /app/data

USER rugguard
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RUGGUARD_DB=/app/data/rugguard.db
VOLUME ["/app/data"]
ENTRYPOINT ["rugguard"]
CMD ["--help"]
