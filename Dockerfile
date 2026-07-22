# syntax=docker/dockerfile:1

FROM python:3.12-slim

# Tesseract is optional (manual captcha entry always works). Installed so the
# OCR pre-fill for captchas works out of the box in the hosted container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    BB_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

# Persistent SQLite + artifacts live here; mount a volume in production.
VOLUME ["/data"]
EXPOSE 8000

# Bind to 0.0.0.0 for container/hosting; no browser auto-open in server mode.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
