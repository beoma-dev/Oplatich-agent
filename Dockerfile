FROM python:3.11-slim

# OCR для автопроверки счетов: Tesseract (rus) + poppler для PDF-сканов.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-rus poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Непривилегированный пользователь: компрометация бота ≠ root в контейнере.
RUN useradd --uid 1000 --create-home app && chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
