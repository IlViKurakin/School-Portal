"""
Модуль для работы с PaddleOCR в облачной среде.
Использует opencv-python-headless для избежания проблем с libGL.
"""

from __future__ import annotations

import os
import sys
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Флаги
IN_CLOUD = os.environ.get("IN_CLOUD", "false").lower() == "true"
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() == "true"
USE_PADDLE_IN_CLOUD = os.environ.get("USE_PADDLE_IN_CLOUD", "true").lower() == "true"

# Кэш
_paddle_engine = None
_paddle_error = None
_paddle_page_cache = {}


def is_paddle_available() -> bool:
    """
    Проверяет, установлены ли необходимые библиотеки Paddle.
    """
    try:
        # Проверяем все необходимые библиотеки
        import setuptools
        import paddle
        import paddleocr
        import numpy
        
        # Проверяем OpenCV (может быть headless)
        try:
            import cv2
        except ImportError:
            # Пробуем headless версию
            try:
                import cv2 as cv2_headless
            except ImportError:
                logger.warning("OpenCV не установлен")
                return False
        
        return True
    except ImportError as e:
        logger.warning(f"PaddleOCR библиотека не установлена: {e}")
        return False


def get_paddle_ocr() -> Optional[Any]:
    """
    Инициализация PaddleOCR с обработкой ошибок.
    """
    global _paddle_engine, _paddle_error
    
    if _paddle_engine is not None:
        return _paddle_engine
    
    if _paddle_error is not None:
        logger.error(f"PaddleOCR недоступен: {_paddle_error}")
        return None
    
    if not OCR_ENABLED:
        _paddle_error = "OCR отключен"
        return None
    
    if not USE_PADDLE_IN_CLOUD:
        _paddle_error = "PaddleOCR отключен в облаке"
        return None
    
    if not is_paddle_available():
        _paddle_error = "PaddleOCR библиотеки не установлены"
        return None
    
    try:
        # Настройка переменных окружения для Paddle
        os.environ["PADDLE_FLAGS"] = "use_mkldnn=0,enable_analysis=0"
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        os.environ["OMP_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "1"
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        
        # Импортируем
        import paddle
        from paddleocr import PaddleOCR
        
        logger.info("Инициализация PaddleOCR...")
        
        # Устанавливаем устройство
        paddle.set_device('cpu')
        
        # Настройки OCR
        ocr_kwargs = {
            "lang": "ru",
            "ocr_version": "PP-OCRv4",
            "text_detection_model_name": "PP-OCRv4_mobile_det",
            "text_recognition_model_name": "eslav_PP-OCRv4_mobile_rec",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": "cpu",
            "engine": "paddle_static",
            "enable_mkldnn": False,
            "cpu_threads": 1,
        }
        
        # Пробуем инициализировать
        _paddle_engine = PaddleOCR(**ocr_kwargs)
        logger.info("PaddleOCR успешно инициализирован")
        return _paddle_engine
        
    except Exception as e:
        _paddle_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Ошибка инициализации PaddleOCR: {_paddle_error}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def ocr_configured() -> bool:
    """Проверяет доступность OCR."""
    if not OCR_ENABLED:
        return False
    engine = get_paddle_ocr()
    return engine is not None


def check_paddle_health() -> Dict[str, Any]:
    """Проверяет работоспособность PaddleOCR."""
    result = {
        "available": False,
        "error": None,
        "version": None,
    }
    
    try:
        import paddleocr
        result["version"] = paddleocr.__version__
        
        engine = get_paddle_ocr()
        if engine is None:
            result["error"] = _paddle_error
        else:
            result["available"] = True
            
    except Exception as e:
        result["error"] = str(e)
    
    return result


def recognize_page(jpeg: bytes) -> str:
    """
    Распознает одну страницу с помощью PaddleOCR.
    """
    global _paddle_page_cache
    
    # Кэширование
    cache_key = (len(jpeg), hash(jpeg))
    cached = _paddle_page_cache.get(cache_key)
    if cached is not None:
        return cached
    
    engine = get_paddle_ocr()
    if engine is None:
        raise RuntimeError("PaddleOCR не доступен")
    
    from PIL import Image, ImageOps
    import io
    
    # Оптимизация изображения
    image = Image.open(io.BytesIO(jpeg))
    image = ImageOps.exif_transpose(image)
    
    if image.mode != "RGB":
        image = image.convert("RGB")
    
    # Уменьшаем для экономии памяти
    max_side = max(image.width, image.height)
    if max_side > 2000:
        scale = 2000.0 / max_side
        new_size = (int(image.width * scale), int(image.height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
            temp_path = temp_file.name
        
        image.save(temp_path, format="JPEG", quality=92)
        
        # Распознавание
        prediction = engine.predict(
            temp_path,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_det_limit_side_len=2000,
            text_det_limit_type="max",
        )
        
        text = _extract_text_from_result(prediction)
        
        if not text.strip():
            # Повтор с другими настройками
            prediction = engine.predict(
                temp_path,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                text_det_limit_side_len=1600,
                text_det_limit_type="max",
            )
            text = _extract_text_from_result(prediction)
        
        if not text.strip():
            raise RuntimeError("PaddleOCR не обнаружил текста")
        
        _paddle_page_cache[cache_key] = text
        return text
        
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _extract_text_from_result(result) -> str:
    """Извлекает текст из результата PaddleOCR."""
    texts = []
    
    def extract_rec_texts(value):
        if isinstance(value, dict):
            rec_texts = value.get("rec_texts")
            if rec_texts and isinstance(rec_texts, (list, tuple)):
                for item in rec_texts:
                    if item and str(item).strip():
                        texts.append(str(item).strip())
            
            for key, child in value.items():
                if key in ["rec_text", "text", "fullText"] and isinstance(child, str) and child.strip():
                    texts.append(child.strip())
                elif key not in ["rec_texts"]:
                    extract_rec_texts(child)
                    
        elif isinstance(value, (list, tuple)):
            for child in value:
                extract_rec_texts(child)
    
    try:
        if hasattr(result, "__iter__"):
            for item in result:
                if hasattr(item, "json"):
                    try:
                        data = item.json() if callable(item.json) else item.json
                        extract_rec_texts(data)
                    except:
                        pass
                
                if hasattr(item, "res"):
                    try:
                        data = item.res
                        extract_rec_texts(data)
                    except:
                        pass
                
                extract_rec_texts(item)
    except:
        extract_rec_texts(result)
    
    # Убираем дубликаты
    seen = set()
    unique_texts = []
    for text in texts:
        if text not in seen:
            seen.add(text)
            unique_texts.append(text)
    
    return "\n".join(unique_texts)


def recognize_multiple_pages(jpegs: List[bytes]) -> str:
    """Распознает несколько страниц."""
    all_texts = []
    
    for i, jpeg in enumerate(jpegs[:6]):
        try:
            text = recognize_page(jpeg)
            if text.strip():
                all_texts.append(text.strip())
        except Exception as e:
            logger.warning(f"Ошибка страницы {i+1}: {e}")
            continue
    
    return "\n".join(all_texts)


def recognize_passport(jpegs: List[bytes]) -> Dict[str, str]:
    """Распознает паспорт."""
    text = recognize_multiple_pages(jpegs)
    if not text.strip():
        return {}
    
    import re
    entities = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    
    def value_after_label(label):
        normalized_label = _normalize_label(label)
        for i, line in enumerate(lines):
            normalized_line = _normalize_label(line)
            if normalized_line == normalized_label:
                if i + 1 < len(lines):
                    return lines[i + 1].strip()
        return ""
    
    surname = value_after_label("фамилия")
    if surname:
        entities["surname"] = surname
    
    first_name = value_after_label("имя")
    if first_name:
        entities["name"] = first_name
    
    middle_name = value_after_label("отчество")
    if middle_name:
        entities["middle_name"] = middle_name
    
    birth_value = value_after_label("дата рождения")
    if birth_value:
        birth_match = re.search(r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b", birth_value)
        if birth_match:
            entities["birth_date"] = f"{birth_match.group(1)}.{birth_match.group(2)}.{birth_match.group(3)}"
    
    compact_text = " ".join(text.split())
    patterns = [
        r"\b(\d{2})\s+(\d{2})\s+(\d{6})\b",
        r"\b(\d{4})\s+(\d{6})\b",
        r"\b(\d{10})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact_text)
        if match:
            number = "".join(match.groups())
            number = re.sub(r"\D", "", number)
            if len(number) == 10:
                entities["number"] = number
                break
    
    normalized_text = _normalize_label(text)
    if "жен" in normalized_text or "женский" in normalized_text:
        entities["gender"] = "Женский"
    elif "муж" in normalized_text or "мужской" in normalized_text:
        entities["gender"] = "Мужской"
    
    return entities


def _normalize_label(value: str) -> str:
    value = str(value or "").lower()
    value = value.replace("ё", "е")
    return re.sub(r"[^а-яa-z0-9]+", " ", value).strip()
