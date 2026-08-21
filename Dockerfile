FROM python:3.11-slim

# Минимальные зависимости
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libgomp1 \
    libatlas-base-dev \
    libopenblas-dev \
    liblapack-dev \
    gfortran \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Обновляем pip
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads generated_documents

ENV PYTHONUNBUFFERED=1
ENV IN_CLOUD=true
ENV OCR_ENABLED=true
ENV USE_PADDLE_IN_CLOUD=true
ENV CUDA_VISIBLE_DEVICES=-1
ENV OMP_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "1", "--timeout", "300", "main:app"]
