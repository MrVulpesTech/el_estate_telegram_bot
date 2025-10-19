FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --upgrade pip && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd -u 10001 -m appuser
COPY --from=builder /wheels /wheels
RUN pip install --no-cache /wheels/*
COPY src ./src
COPY requirements.txt ./
RUN chown -R appuser:appuser /app
USER appuser
ENV PYTHONPATH=/app
CMD ["python", "-m", "src.app"]

