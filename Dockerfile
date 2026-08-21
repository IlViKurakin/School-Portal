FROM python:3.11-slim

# Устанавливаем все системные зависимости для PaddleOCR
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libatlas-base-dev \
    libopenblas-dev \
    liblapack-dev \
    gfortran \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Обновляем pip и устанавливаем setuptools (ВАЖНО!)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

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
ENV PADDLE_FLAGS=use_mkldnn=0,enable_analysis=0
ENV CUDA_VISIBLE_DEVICES=-1
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1

EXPOSE 5000

# Увеличиваем время загрузки для PaddleOCR
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--timeout", "300", "main:app"]
