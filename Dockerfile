FROM python:3.11-slim

# Устанавливаем все системные зависимости для PaddleOCR
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libglib2.0-0 \
    libatlas-base-dev \
    libopenblas-dev \
    liblapack-dev \
    gfortran \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

RUN mkdir -p uploads generated_documents

ENV PYTHONUNBUFFERED=1
ENV IN_CLOUD=true
ENV OCR_ENABLED=true
ENV USE_PADDLE_IN_CLOUD=true

EXPOSE 5000

# Увеличиваем время загрузки для PaddleOCR
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "2", "--timeout", "300", "main:app"]
