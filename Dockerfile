FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng \
    libglib2.0-0 libsm6 libxext6 libxrender1 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Samara
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
