FROM python:3.12-slim

WORKDIR /workspace/platform/backend

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

COPY platform/backend/pyproject.toml ./pyproject.toml
COPY platform/backend/app ./app
COPY runtime /workspace/runtime

RUN pip install --no-cache-dir -e .
RUN python -m playwright install --with-deps chromium

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
