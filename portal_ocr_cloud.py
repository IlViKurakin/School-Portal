"""
Модуль для работы с PaddleOCR в облачной среде.
Обеспечивает безопасную инициализацию и обработку ошибок.
"""

from __future__ import annotations

import os
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Флаг, указывающий, что мы в облачной среде
IN_CLOUD = os.environ.get("IN_CLOUD", "false").lower() == "true"

# Флаг для отключения OCR в облаке при проблемах
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() == "true"

# Кэш для экземпляра PaddleOCR
_paddle_engine = None
_paddle_error = None


def is_paddle_available() -> bool:
    """
    Проверяет, установлены ли необходимые библиотеки Paddle.
    """
    try:
        import paddle
        import paddleocr
        import numpy
        return True
    except ImportError as e:
        logger.warning(f"PaddleOCR не установлен: {e}")
        return False


def get_paddle_ocr_safe() -> Optional[Any]:
    """
    Безопасная инициализация PaddleOCR с обработкой ошибок
    для облачной среды.
    
    Returns:
        Экземпляр PaddleOCR или None при ошибке
    """
    global _paddle_engine, _paddle_error
    
    if _paddle_engine is not None:
        return _paddle_engine
    
    if _paddle_error is not None:
        logger.error(f"PaddleOCR недоступен: {_paddle_error}")
        return None
    
    if not OCR_ENABLED:
        _paddle_error = "OCR отключен переменной OCR_ENABLED"
        logger.info(_paddle_error)
        return None
    
    if not is_paddle_available():
        _paddle_error = "PaddleOCR библиотеки не установлены"
        return None
    
    try:
        # Импортируем внутри функции для отложенной загрузки
        import paddle
        import paddleocr
        import numpy as np
        from paddleocr import PaddleOCR
        
        logger.info("Инициализация PaddleOCR...")
        
        # Настройка для облачной среды
        if IN_CLOUD:
            # Ограничиваем использование памяти
            paddle.set_device('cpu')
            os.environ["PADDLE_FLAGS"] = "use_mkldnn=0,enable_analysis=0"
        
        # Общие настройки
        ocr_kwargs = {
            "lang": "ru",
            "ocr_version": "PP-OCRv4",  # Используем более легкую версию
            "text_detection_model_name": "PP-OCRv4_mobile_det",
            "text_recognition_model_name": "eslav_PP-OCRv4_mobile_rec",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": "cpu",
            "engine": "paddle_static",
            "enable_mkldnn": False,
            "cpu_threads": 2,
        }
        
        # Уменьшаем нагрузку в облаке
        if IN_CLOUD:
            ocr_kwargs["cpu_threads"] = 1
            logger.info("Облачный режим: ограничение ресурсов PaddleOCR")
        
        _paddle_engine = PaddleOCR(**ocr_kwargs)
        logger.info("PaddleOCR успешно инициализирован")
        return _paddle_engine
        
    except Exception as e:
        _paddle_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Ошибка инициализации PaddleOCR: {_paddle_error}")
        return None


def ocr_configured() -> bool:
    """
    Проверяет, настроен ли OCR для работы.
    """
    if not OCR_ENABLED:
        return False
    
    if IN_CLOUD and not is_paddle_available():
        return False
    
    # Проверяем, что PaddleOCR может быть инициализирован
    engine = get_paddle_ocr_safe()
    return engine is not None


def reset_paddle_engine():
    """
    Сбрасывает кэш PaddleOCR (для тестирования).
    """
    global _paddle_engine, _paddle_error
    _paddle_engine = None
    _paddle_error = None