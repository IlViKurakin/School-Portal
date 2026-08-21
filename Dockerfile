FROM python:3.11-slim

# Устанавливаем ВСЕ необходимые системные зависимости
RUN apt-get update && apt-get install -y \
    # Базовые зависимости
    build-essential \
    wget \
    curl \
    git \
    # OpenGL и графические библиотеки (для OpenCV)
    libgl1-mesa-glx \
    libgl1-mesa-dri \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    # Дополнительные библиотеки OpenCV
    libgtk2.0-dev \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libv4l-dev \
    libxvidcore-dev \
    libx264-dev \
    # Математические библиотеки
    libatlas-base-dev \
    libopenblas-dev \
    liblapack-dev \
    gfortran \
    # Утилиты
    ffmpeg \
    libfreetype6-dev \
    libpng-dev \
    libjpeg-dev \
    libtiff-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Обновляем pip и устанавливаем setuptools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

RUN mkdir -p uploads generated_documents

# Переменные окружения
ENV PYTHONUNBUFFERED=1
ENV IN_CLOUD=true
ENV OCR_ENABLED=true
ENV USE_PADDLE_IN_CLOUD=true
ENV PADDLE_FLAGS=use_mkldnn=0,enable_analysis=0
ENV CUDA_VISIBLE_DEVICES=-1
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1
ENV LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "1", "--timeout", "300", "main:app"]
