"""
Модуль для работы с PaddleOCR в облачной среде.
"""

from __future__ import annotations

import os
import sys
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Флаг, указывающий, что мы в облачной среде
IN_CLOUD = os.environ.get("IN_CLOUD", "false").lower() == "true"

# Флаг для отключения OCR
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() == "true"

# Использовать ли PaddleOCR в облаке
USE_PADDLE_IN_CLOUD = os.environ.get("USE_PADDLE_IN_CLOUD", "true").lower() == "true"

# Кэш для экземпляра PaddleOCR
_paddle_engine = None
_paddle_error = None
_paddle_page_cache = {}


def is_paddle_available() -> bool:
    """
    Проверяет, установлены ли необходимые библиотеки Paddle.
    """
    try:
        # Проверяем setuptools
        import setuptools
        import paddle
        import paddleocr
        import numpy
        import cv2
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
        _paddle_error = "OCR отключен переменной OCR_ENABLED"
        logger.info(_paddle_error)
        return None
    
    # В облаке используем Paddle только если явно разрешено
    if IN_CLOUD and not USE_PADDLE_IN_CLOUD:
        _paddle_error = "PaddleOCR отключен в облаке"
        return None
    
    if not is_paddle_available():
        _paddle_error = "PaddleOCR библиотеки не установлены"
        return None
    
    try:
        # Импортируем с отложенной загрузкой
        import paddle
        from paddleocr import PaddleOCR
        
        logger.info("Инициализация PaddleOCR...")
        
        # Настройка Paddle для облачной среды
        if IN_CLOUD:
            # Устанавливаем устройство CPU
            paddle.set_device('cpu')
            
            # Ограничиваем использование памяти
            os.environ["PADDLE_FLAGS"] = "use_mkldnn=0,enable_analysis=0"
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
            
            # Ограничиваем потоки
            os.environ["OMP_NUM_THREADS"] = "1"
            os.environ["MKL_NUM_THREADS"] = "1"
            os.environ["OPENBLAS_NUM_THREADS"] = "1"
        
        # Оптимальные настройки для облачной среды
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
        
        # Пробуем инициализировать с базовыми настройками
        try:
            _paddle_engine = PaddleOCR(**ocr_kwargs)
        except Exception as e:
            logger.warning(f"Ошибка при инициализации с основными настройками: {e}")
            # Пробуем с минимальными настройками
            min_kwargs = {
                "lang": "ru",
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "device": "cpu",
                "cpu_threads": 1,
            }
            _paddle_engine = PaddleOCR(**min_kwargs)
        
        logger.info("PaddleOCR успешно инициализирован")
        return _paddle_engine
        
    except Exception as e:
        _paddle_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Ошибка инициализации PaddleOCR: {_paddle_error}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def clear_paddle_cache():
    """
    Очищает кэш PaddleOCR.
    """
    global _paddle_page_cache
    _paddle_page_cache = {}
    logger.info("Кэш PaddleOCR очищен")


def ocr_configured() -> bool:
    """
    Проверяет, доступен ли PaddleOCR.
    """
    if not OCR_ENABLED:
        return False
    
    # Проверяем, что PaddleOCR может быть инициализирован
    engine = get_paddle_ocr()
    return engine is not None


def reset_paddle_engine():
    """
    Сбрасывает кэш PaddleOCR (для тестирования).
    """
    global _paddle_engine, _paddle_error, _paddle_page_cache
    _paddle_engine = None
    _paddle_error = None
    _paddle_page_cache = {}
    logger.info("PaddleOCR сброшен")


def check_paddle_health() -> Dict[str, Any]:
    """
    Проверяет работоспособность PaddleOCR.
    """
    result = {
        "available": False,
        "error": None,
        "version": None,
        "memory_mb": None,
    }
    
    try:
        import paddle
        import paddleocr
        
        result["available"] = True
        result["version"] = paddleocr.__version__
        
        # Проверяем использование памяти
        try:
            import psutil
            process = psutil.Process()
            result["memory_mb"] = process.memory_info().rss / 1024 / 1024
        except:
            pass
        
        # Пытаемся инициализировать
        engine = get_paddle_ocr()
        if engine is None:
            result["available"] = False
            result["error"] = _paddle_error
            
    except Exception as e:
        result["available"] = False
        result["error"] = str(e)
    
    return result


# ============================================================
# Распознавание с PaddleOCR
# ============================================================

def recognize_page(jpeg: bytes) -> str:
    """
    Распознает одну страницу с помощью PaddleOCR.
    """
    global _paddle_page_cache
    
    # Кэширование результатов
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
    
    # Конвертируем в RGB если нужно
    if image.mode != "RGB":
        image = image.convert("RGB")
    
    # Уменьшаем большие изображения для экономии памяти
    max_side = max(image.width, image.height)
    if max_side > 2000:
        scale = 2000.0 / max_side
        new_size = (int(image.width * scale), int(image.height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    
    # Сохраняем во временный файл
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
        
        # Извлечение текста из результата
        text = _extract_text_from_result(prediction)
        
        if not text.strip():
            # Пробуем с другими настройками
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
            raise RuntimeError("PaddleOCR не обнаружил текста на странице")
        
        # Кэшируем результат
        _paddle_page_cache[cache_key] = text
        return text
        
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _extract_text_from_result(result) -> str:
    """
    Извлекает текст из результата PaddleOCR.
    """
    texts = []
    
    def extract_rec_texts(value):
        if isinstance(value, dict):
            rec_texts = value.get("rec_texts")
            if rec_texts and isinstance(rec_texts, (list, tuple)):
                for item in rec_texts:
                    if item and str(item).strip():
                        texts.append(str(item).strip())
            
            # Проверяем другие поля
            for key, child in value.items():
                if key in ["rec_text", "text", "fullText"] and isinstance(child, str) and child.strip():
                    texts.append(child.strip())
                elif key not in ["rec_texts"]:
                    extract_rec_texts(child)
                    
        elif isinstance(value, (list, tuple)):
            for child in value:
                extract_rec_texts(child)
    
    # Прямой доступ к результатам
    try:
        if hasattr(result, "__iter__"):
            for item in result:
                # Проверяем разные форматы
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
                
                # Прямой рекурсивный обход
                extract_rec_texts(item)
    except:
        # Если итерация не работает, обрабатываем как есть
        extract_rec_texts(result)
    
    # Убираем дубликаты, сохраняя порядок
    seen = set()
    unique_texts = []
    for text in texts:
        if text not in seen:
            seen.add(text)
            unique_texts.append(text)
    
    return "\n".join(unique_texts)


def recognize_multiple_pages(jpegs: List[bytes]) -> str:
    """
    Распознает несколько страниц.
    """
    all_texts = []
    
    for i, jpeg in enumerate(jpegs[:6]):  # Ограничиваем 6 страницами
        try:
            text = recognize_page(jpeg)
            if text.strip():
                all_texts.append(text.strip())
                logger.debug(f"Страница {i+1} распознана: {len(text)} символов")
        except Exception as e:
            logger.warning(f"Ошибка распознавания страницы {i+1}: {e}")
            continue
    
    return "\n".join(all_texts)


def recognize_passport(jpegs: List[bytes]) -> Dict[str, str]:
    """
    Распознает паспорт с помощью PaddleOCR.
    """
    # Используем общее распознавание
    text = recognize_multiple_pages(jpegs)
    if not text.strip():
        return {}
    
    # Извлекаем данные из текста
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
    
    # ФИО
    surname = value_after_label("фамилия")
    if surname:
        entities["surname"] = surname
    
    first_name = value_after_label("имя")
    if first_name:
        entities["name"] = first_name
    
    middle_name = value_after_label("отчество")
    if middle_name:
        entities["middle_name"] = middle_name
    
    # Дата рождения
    birth_value = value_after_label("дата рождения")
    if birth_value:
        birth_match = re.search(r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b", birth_value)
        if birth_match:
            entities["birth_date"] = f"{birth_match.group(1)}.{birth_match.group(2)}.{birth_match.group(3)}"
    
    # Номер паспорта
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
    
    # Пол
    normalized_text = _normalize_label(text)
    if "жен" in normalized_text or "женский" in normalized_text:
        entities["gender"] = "Женский"
    elif "муж" in normalized_text or "мужской" in normalized_text:
        entities["gender"] = "Мужской"
    
    return entities


def recognize_snils(jpegs: List[bytes]) -> Dict[str, Any]:
    """
    Распознает СНИЛС с помощью PaddleOCR.
    """
    text = recognize_multiple_pages(jpegs)
    if not text.strip():
        return {"detected": "unknown", "confidence": 0.0, "snils": "", "text": ""}
    
    # Ищем СНИЛС в тексте
    import re
    pattern = re.compile(
        r"(?<!\d)"
        r"(\d{3})"
        r"\D{0,3}"
        r"(\d{3})"
        r"\D{0,3}"
        r"(\d{3})"
        r"\D{0,3}"
        r"(\d{2})"
        r"(?!\d)"
    )
    
    numbers = []
    for match in pattern.finditer(text):
        number = "".join(match.groups())
        formatted = f"{number[:3]}-{number[3:6]}-{number[6:9]} {number[9:]}"
        if formatted not in numbers:
            numbers.append(formatted)
    
    # Проверяем контрольное число
    valid_number = None
    for number in numbers:
        if _check_snils_checksum(number):
            valid_number = number
            break
    
    # Проверяем наличие ключевых слов
    normalized = _normalize_label(text)
    keywords = ["снилс", "страхов", "лицевого счета", "пенсионного"]
    keyword_hits = sum(1 for kw in keywords if kw in normalized)
    
    if valid_number and keyword_hits > 0:
        return {"detected": "snils", "confidence": 0.99, "snils": valid_number, "text": text}
    elif valid_number:
        return {"detected": "snils", "confidence": 0.90, "snils": valid_number, "text": text}
    elif numbers and keyword_hits > 0:
        return {"detected": "unknown", "confidence": 0.68, "snils": numbers[0], "text": text}
    elif keyword_hits > 0:
        return {"detected": "unknown", "confidence": 0.50, "snils": "", "text": text}
    else:
        return {"detected": "unknown", "confidence": 0.0, "snils": "", "text": text}


def _normalize_label(value: str) -> str:
    """Нормализует текст для поиска."""
    value = str(value or "").lower()
    value = value.replace("ё", "е")
    return re.sub(r"[^а-яa-z0-9]+", " ", value).strip()


def _check_snils_checksum(value: str) -> bool:
    """Проверяет контрольное число СНИЛС."""
    number = re.sub(r"\D", "", value or "")
    if len(number) != 11:
        return False
    
    first_nine = number[:9]
    try:
        base_number = int(first_nine)
    except ValueError:
        return False
    
    # Для номеров до 1001998 контрольная сумма не проверяется
    if base_number <= 1001998:
        return True
    
    total = sum(int(d) * w for d, w in zip(first_nine, range(9, 0, -1)))
    
    if total < 100:
        expected = total
    elif total in (100, 101):
        expected = 0
    else:
        expected = total % 101
        if expected == 100:
            expected = 0
    
    actual = int(number[-2:])
    return actual == expected


def recognize_birth_certificate(jpegs: List[bytes]) -> Dict[str, str]:
    """
    Распознает свидетельство о рождении с помощью PaddleOCR.
    """
    text = recognize_multiple_pages(jpegs)
    if not text.strip():
        return {}
    
    import re
    result = {}
    
    # Серия и номер свидетельства
    pattern = re.compile(
        r"([IVXLCDM1]{1,6})\s*[-–—]?\s*([А-ЯЁA-Z]{2})\s*(?:№|N|#)?\s*(\d{6})",
        re.IGNORECASE
    )
    match = pattern.search(text)
    if match:
        roman = match.group(1).upper().replace("1", "I")
        letters = match.group(2).upper()
        number = match.group(3)
        
        # Транслитерация
        trans = str.maketrans({
            "T": "Т", "H": "Н", "N": "Н", "K": "К",
            "M": "М", "C": "С", "B": "В", "P": "Р",
            "A": "А", "E": "Е", "O": "О", "X": "Х"
        })
        letters = letters.translate(trans)
        
        result["series"] = f"{roman}-{letters}"
        result["number"] = number
    
    # Дата рождения
    date_pattern = re.compile(r"(\d{2})[./-](\d{2})[./-](\d{4})")
    date_match = date_pattern.search(text)
    if date_match:
        try:
            from datetime import datetime
            parsed = datetime.strptime(
                f"{date_match.group(1)}.{date_match.group(2)}.{date_match.group(3)}",
                "%d.%m.%Y"
            )
            result["birth_date"] = parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass
    
    # ФИО ребенка
    name_pattern = re.compile(
        r"выдан[оа]?\s*([А-ЯЁ][А-ЯЁа-яё-]{1,})\s+([А-ЯЁ][А-ЯЁа-яё-]{1,})\s+([А-ЯЁ][А-ЯЁа-яё-]{1,})",
        re.IGNORECASE
    )
    name_match = name_pattern.search(text)
    if name_match:
        result["last_name"] = name_match.group(1)
        result["first_name"] = name_match.group(2)
        result["middle_name"] = name_match.group(3)
    
    return result
