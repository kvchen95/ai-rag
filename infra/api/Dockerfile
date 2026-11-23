FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY infra/api/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

COPY infra/api/placeholder_main.py /opt/placeholder/placeholder_main.py

EXPOSE 8000

CMD ["uvicorn", "placeholder_main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/opt/placeholder"]
