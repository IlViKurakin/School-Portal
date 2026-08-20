# BIRTH CERTIFICATE SMART OCR V6.6
# DOCUMENT RULES V6.5
# CLOUD COMPATIBLE V6.6.1

from __future__ import annotations

import base64
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz
import requests
from flask import (
    abort,
    flash,
    g,
    redirect,
    request,
    url_for,
)
from PIL import (
    Image,
    ImageFile,
    ImageOps,
    UnidentifiedImageError,
)
from pypdf import PdfReader, PdfWriter

# Импортируем облачный модуль OCR
from portal_ocr_cloud import (
    get_paddle_ocr_safe,
    ocr_configured,
    IN_CLOUD,
    OCR_ENABLED,
    is_paddle_available,
)

ImageFile.LOAD_TRUNCATED_IMAGES = True


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
}

MAX_SINGLE_FILE_SIZE = 25 * 1024 * 1024
MAX_FILES_PER_DOCUMENT = 30
MAX_OCR_PAGES = 6


AUTO_VALIDATION_TYPES = {
    "parent_passport",
    "child_passport",
    "parent_snils",
    "child_snils",
    "birth_certificate",
    "attachment_application",
    "withdrawal_application",
    "parent_consent",
    "education_notice",
}


DETECTED_LABELS = {
    "passport": "Паспорт",
    "snils": "СНИЛС",
    "birth_certificate": "Свидетельство о рождении",
    "attachment_application": "Заявление на прикрепление",
    "withdrawal_application": "Заявление на отчисление",
    "parent_consent": "Согласие законного представителя",
    "education_notice": "Уведомление о выборе формы образования",
    "unknown": "Не удалось определить",
}


EXPECTED_FAMILY = {
    "parent_passport": "passport",
    "child_passport": "passport",
    "parent_snils": "snils",
    "child_snils": "snils",
    "birth_certificate": "birth_certificate",
    "attachment_application": "attachment_application",
    "withdrawal_application": "withdrawal_application",
    "parent_consent": "parent_consent",
    "education_notice": "education_notice",
}


def apply_validation_v6(
    app,
    namespace: dict[str, Any],
) -> None:

    database_path: Path = namespace["DATABASE_PATH"]
    upload_dir: Path = namespace["UPLOAD_DIR"]

    get_db = namespace["get_db"]
    get_student_or_404 = namespace[
        "get_student_or_404"
    ]
    roles_required = namespace["roles_required"]
    login_required = namespace["login_required"]
    audit = namespace["audit"]
    render_page = namespace["render_page"]
    document_types = namespace["DOCUMENT_TYPES"]
    document_is_required = namespace[
        "document_is_required"
    ]

    # ========================================================
    # База данных
    # ========================================================

    def migrate_database() -> None:
        if not database_path.exists():
            return

        db = sqlite3.connect(database_path)

        try:
            columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }

            migrations = {
                "page_count":
                    "ALTER TABLE documents "
                    "ADD COLUMN page_count "
                    "INTEGER NOT NULL DEFAULT 1",

                "ocr_text":
                    "ALTER TABLE documents "
                    "ADD COLUMN ocr_text TEXT",

                "ocr_json":
                    "ALTER TABLE documents "
                    "ADD COLUMN ocr_json TEXT",

                "validation_status":
                    "ALTER TABLE documents "
                    "ADD COLUMN validation_status "
                    "TEXT NOT NULL DEFAULT 'not_checked'",

                "detected_document_type":
                    "ALTER TABLE documents "
                    "ADD COLUMN detected_document_type TEXT",

                "validation_confidence":
                    "ALTER TABLE documents "
                    "ADD COLUMN validation_confidence REAL",

                "validation_message":
                    "ALTER TABLE documents "
                    "ADD COLUMN validation_message TEXT",

                "validation_details":
                    "ALTER TABLE documents "
                    "ADD COLUMN validation_details TEXT",

                "validated_at":
                    "ALTER TABLE documents "
                    "ADD COLUMN validated_at TEXT",

                "autofill_applied":
                    "ALTER TABLE documents "
                    "ADD COLUMN autofill_applied "
                    "INTEGER NOT NULL DEFAULT 0",

                "autofill_blocked":
                    "ALTER TABLE documents "
                    "ADD COLUMN autofill_blocked "
                    "INTEGER NOT NULL DEFAULT 0",
            }

            for column, sql in migrations.items():
                if column not in columns:
                    db.execute(sql)

            db.commit()

        finally:
            db.close()

    previous_init_db = namespace["init_db"]

    def init_db_v6() -> None:
        previous_init_db()
        migrate_database()

    namespace["init_db"] = init_db_v6

    if database_path.exists():
        migrate_database()

    # ========================================================
    # Чек-лист
    # ========================================================

    def build_document_checklist_v6(student):
        latest = {
            row["document_type"]: row
            for row in get_db().execute(
                """
                SELECT d.*
                FROM documents d
                JOIN (
                    SELECT
                        document_type,
                        MAX(version) AS latest_version
                    FROM documents
                    WHERE student_id = ?
                      AND save_confirmed = 1
                    GROUP BY document_type
                ) x
                    ON x.document_type = d.document_type
                   AND x.latest_version = d.version
                WHERE d.student_id = ?
                  AND d.save_confirmed = 1
                """,
                (
                    student["id"],
                    student["id"],
                ),
            ).fetchall()
        }

        result = []

        for code, config in document_types.items():
            document = latest.get(code)

            validation_status = (
                document["validation_status"]
                if document
                else None
            )

            invalid_document = None

            if (
                document
                and validation_status == "mismatch"
            ):
                invalid_document = document
                document = None

            result.append(
                {
                    "code": code,
                    "name": config["name"],
                    "group": config["group"],
                    "help": config.get(
                        "help",
                        "",
                    ),
                    "required": document_is_required(
                        student,
                        config["rule"],
                    ),
                    "document": document,
                    "invalid_document":
                        invalid_document,
                    "validation_status":
                        validation_status,
                }
            )

        return result

    def missing_required_documents_v6(student):
        return [
            item["name"]
            for item in build_document_checklist_v6(
                student
            )
            if (
                item["required"]
                and not item["document"]
            )
        ]

    namespace[
        "build_document_checklist"
    ] = build_document_checklist_v6

    namespace[
        "missing_required_documents"
    ] = missing_required_documents_v6

    # ========================================================
    # Файлы
    # ========================================================

    def clean_original_name(filename: str) -> str:
        filename = str(
            filename or ""
        ).replace("\\", "/")

        return (
            Path(filename).name.strip()
            or "document"
        )

    def extension_from_name(
        filename: str,
    ) -> str:
        return Path(
            clean_original_name(filename)
        ).suffix.lower()

    def validate_upload(upload):
        original_name = clean_original_name(
            upload.filename
        )

        extension = extension_from_name(
            original_name
        )

        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Файл «{original_name}»: "
                "разрешены только PDF, JPG, "
                "JPEG и PNG."
            )

        content = upload.read()

        if not content:
            raise ValueError(
                f"Файл «{original_name}» пустой."
            )

        if len(content) > MAX_SINGLE_FILE_SIZE:
            raise ValueError(
                f"Файл «{original_name}» "
                "больше 25 МБ."
            )

        pdf_position = content[:1024].find(
            b"%PDF"
        )

        if extension == ".pdf":
            if pdf_position < 0:
                raise ValueError(
                    f"Файл «{original_name}» "
                    "не является корректным PDF."
                )

            content = content[pdf_position:]

            try:
                reader = PdfReader(
                    io.BytesIO(content)
                )

                if reader.is_encrypted:
                    raise ValueError(
                        f"Файл «{original_name}» "
                        "защищен паролем."
                    )

                if not reader.pages:
                    raise ValueError(
                        f"Файл «{original_name}» "
                        "не содержит страниц."
                    )

            except ValueError:
                raise

            except Exception as error:
                raise ValueError(
                    f"Не удалось прочитать "
                    f"«{original_name}»: {error}"
                ) from error

            return (
                original_name,
                content,
                "pdf",
            )

        try:
            image = Image.open(
                io.BytesIO(content)
            )

            actual_format = str(
                image.format or ""
            ).upper()

            if actual_format not in {
                "JPEG",
                "JPG",
                "PNG",
            }:
                raise ValueError(
                    f"Файл «{original_name}» "
                    f"имеет формат "
                    f"{actual_format or 'неизвестный'}."
                )

            image.load()

        except ValueError:
            raise

        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
        ) as error:
            raise ValueError(
                f"Не удалось прочитать "
                f"изображение «{original_name}»: "
                f"{error}"
            ) from error

        return (
            original_name,
            content,
            "image",
        )

    def image_to_pdf_reader(
        content: bytes,
    ) -> PdfReader:
        image = Image.open(
            io.BytesIO(content)
        )

        image = ImageOps.exif_transpose(
            image
        )

        if image.mode != "RGB":
            image = image.convert("RGB")

        output = io.BytesIO()

        image.save(
            output,
            format="PDF",
            resolution=150.0,
        )

        output.seek(0)

        return PdfReader(output)

    def merge_files(uploads):
        if len(uploads) > MAX_FILES_PER_DOCUMENT:
            raise ValueError(
                "За один раз можно загрузить "
                "не более 30 файлов."
            )

        writer = PdfWriter()
        original_names = []
        page_count = 0

        for upload in uploads:
            (
                original_name,
                content,
                file_kind,
            ) = validate_upload(upload)

            original_names.append(
                original_name
            )

            if file_kind == "pdf":
                reader = PdfReader(
                    io.BytesIO(content)
                )

            else:
                reader = image_to_pdf_reader(
                    content
                )

            for page in reader.pages:
                writer.add_page(page)
                page_count += 1

        if not page_count:
            raise ValueError(
                "В выбранных файлах "
                "не найдено страниц."
            )

        output = io.BytesIO()
        writer.write(output)

        return (
            output.getvalue(),
            original_names,
            page_count,
        )

    # ========================================================
    # OCR - ОБНОВЛЕННАЯ ВЕРСИЯ ДЛЯ ОБЛАКА
    # ========================================================

    # Кэш для PaddleOCR и ошибок
    _paddle_ocr_engine = None
    _paddle_ocr_error = None
    _paddle_page_cache = {}

    def ocr_configured_local() -> bool:
        """
        Проверяем наличие локального PaddleOCR
        с использованием облачного модуля.
        """
        return ocr_configured()

    def _get_paddle_ocr():
        """
        Получение экземпляра PaddleOCR с использованием
        облачного модуля.
        """
        nonlocal _paddle_ocr_engine
        nonlocal _paddle_ocr_error

        if _paddle_ocr_engine is not None:
            return _paddle_ocr_engine

        if _paddle_ocr_error is not None:
            raise RuntimeError(_paddle_ocr_error)

        # Используем безопасную инициализацию из облачного модуля
        engine = get_paddle_ocr_safe()
        
        if engine is None:
            _paddle_ocr_error = "PaddleOCR не доступен в облачной среде"
            raise RuntimeError(_paddle_ocr_error)

        _paddle_ocr_engine = engine
        return _paddle_ocr_engine

    def _extract_rec_texts(value):
        """
        Извлекает rec_texts из результата
        PaddleOCR 3.x.
        """
        texts = []

        if isinstance(value, dict):

            rec_texts = value.get(
                "rec_texts"
            )

            if isinstance(
                rec_texts,
                (list, tuple),
            ):
                for item in rec_texts:
                    if item is None:
                        continue

                    text = str(
                        item
                    ).strip()

                    if text:
                        texts.append(
                            text
                        )

            for child in value.values():
                texts.extend(
                    _extract_rec_texts(
                        child
                    )
                )

        elif isinstance(
            value,
            (list, tuple),
        ):
            for child in value:
                texts.extend(
                    _extract_rec_texts(
                        child
                    )
                )

        return texts

    def _paddle_result_text(result):
        """
        Поддерживаем несколько вариантов
        объекта результата PaddleOCR 3.x.
        """
        texts = []

        for item in result:

            candidates = []

            # Сам объект
            candidates.append(
                item
            )

            # item.json
            try:
                data = getattr(
                    item,
                    "json",
                    None,
                )

                if callable(data):
                    data = data()

                if isinstance(data, str):
                    try:
                        data = json.loads(
                            data
                        )
                    except Exception:
                        pass

                if data is not None:
                    candidates.append(
                        data
                    )

            except Exception:
                pass

            # item.res
            try:
                data = getattr(
                    item,
                    "res",
                    None,
                )

                if data is not None:
                    candidates.append(
                        data
                    )

            except Exception:
                pass

            for candidate in candidates:

                found = (
                    _extract_rec_texts(
                        candidate
                    )
                )

                if found:
                    texts.extend(
                        found
                    )

        # Убираем дубликаты,
        # сохраняя исходный порядок.
        result_texts = []

        seen = set()

        for text in texts:

            key = text.strip()

            if (
                key
                and key not in seen
            ):
                seen.add(key)
                result_texts.append(
                    key
                )

        return "\n".join(
            result_texts
        )

    def _recognize_local_page(
        jpeg: bytes,
    ) -> str:
        """
        Распознает одну страницу с помощью PaddleOCR.
        """
        import tempfile

        cache_key = (
            len(jpeg),
            hash(jpeg),
        )

        cached = _paddle_page_cache.get(
            cache_key
        )

        if cached is not None:
            return cached

        # -----------------------------------
        # Нормализуем изображение.
        # -----------------------------------

        image = Image.open(
            io.BytesIO(jpeg)
        )

        image = ImageOps.exif_transpose(
            image
        )

        if image.mode != "RGB":
            image = image.convert(
                "RGB"
            )

        # Очень большие фото со смартфонов
        # уменьшаем перед OCR.
        max_side = max(
            image.width,
            image.height,
        )

        if max_side > 2400:
            scale = (
                2400.0 / max_side
            )

            new_size = (
                max(
                    1,
                    int(
                        image.width
                        * scale
                    ),
                ),
                max(
                    1,
                    int(
                        image.height
                        * scale
                    ),
                ),
            )

            image = image.resize(
                new_size
            )

        temp_path = None

        try:
            # На Windows для Paddle
            # надежнее передавать путь
            # к реальному JPG.
            with tempfile.NamedTemporaryFile(
                suffix=".jpg",
                delete=False,
            ) as temp_file:

                temp_path = (
                    temp_file.name
                )

            image.save(
                temp_path,
                format="JPEG",
                quality=92,
            )

            engine = _get_paddle_ocr()

            try:
                prediction = engine.predict(
                    temp_path,

                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,

                    # Ограничиваем размер,
                    # поступающий в detector.
                    text_det_limit_side_len=2400,
                    text_det_limit_type="max",
                )

            except Exception as first_error:

                print(
                    "Первый запуск OCR "
                    "завершился ошибкой:",
                    repr(first_error),
                )

                print(
                    "Повтор OCR "
                    "с уменьшенным изображением..."
                )

                # --------------------------------
                # Второй, более консервативный
                # вариант.
                # --------------------------------

                image_retry = image.copy()

                retry_max = max(
                    image_retry.width,
                    image_retry.height,
                )

                if retry_max > 1600:
                    retry_scale = (
                        1600.0
                        / retry_max
                    )

                    image_retry = (
                        image_retry.resize(
                            (
                                max(
                                    1,
                                    int(
                                        image_retry.width
                                        * retry_scale
                                    ),
                                ),
                                max(
                                    1,
                                    int(
                                        image_retry.height
                                        * retry_scale
                                    ),
                                ),
                            )
                        )
                    )

                image_retry.save(
                    temp_path,
                    format="JPEG",
                    quality=88,
                )

                try:
                    prediction = (
                        engine.predict(
                            temp_path,

                            use_doc_orientation_classify=False,
                            use_doc_unwarping=False,
                            use_textline_orientation=False,

                            text_det_limit_side_len=1600,
                            text_det_limit_type="max",
                        )
                    )

                except Exception as second_error:

                    raise RuntimeError(
                        "PaddleOCR не смог "
                        "обработать изображение. "
                        "Первая попытка: "
                        f"{type(first_error).__name__}: "
                        f"{first_error!r}. "
                        "Повторная попытка: "
                        f"{type(second_error).__name__}: "
                        f"{second_error!r}."
                    ) from second_error

            text = _paddle_result_text(
                prediction
            )

            if not text.strip():
                raise RuntimeError(
                    "PaddleOCR обработал файл, "
                    "но не обнаружил "
                    "распознаваемого текста."
                )

            _paddle_page_cache[
                cache_key
            ] = text

            return text

        finally:
            if (
                temp_path
                and
                os.path.exists(
                    temp_path
                )
            ):
                try:
                    os.unlink(
                        temp_path
                    )
                except OSError:
                    pass

    # ========================================================
    # Остальные функции OCR (без изменений)
    # ========================================================

    def general_ocr(jpegs):
        """
        Распознает все страницы документа
        полностью локально.
        """
        # Проверяем доступность OCR
        if not ocr_configured_local():
            raise RuntimeError(
                "OCR не доступен. Проверьте установку PaddleOCR "
                "или настройки облачной среды."
            )
        
        page_texts = []

        for jpeg in jpegs:

            text = _recognize_local_page(
                jpeg
            )

            if text.strip():
                page_texts.append(
                    text.strip()
                )

        return "\n".join(
            page_texts
        )

    def _clean_ocr_line(value):
        value = str(
            value or ""
        ).strip()

        value = re.sub(
            r"\s+",
            " ",
            value,
        )

        return value

    def _normalize_ocr_label(value):
        value = _clean_ocr_line(
            value
        ).lower().replace(
            "ё",
            "е",
        )

        value = re.sub(
            r"[^а-яa-z0-9]+",
            " ",
            value,
        )

        return value.strip()

    def _value_after_label(
        lines,
        label,
    ):
        normalized_label = (
            _normalize_ocr_label(
                label
            )
        )

        for index, line in enumerate(
            lines
        ):
            normalized_line = (
                _normalize_ocr_label(
                    line
                )
            )

            if (
                normalized_line
                == normalized_label
            ):
                if index + 1 < len(lines):
                    return _clean_ocr_line(
                        lines[
                            index + 1
                        ]
                    )

            prefix = (
                normalized_label
                + " "
            )

            if normalized_line.startswith(
                prefix
            ):
                # Например:
                # "Фамилия ИВАНОВА"
                words = _clean_ocr_line(
                    line
                ).split()

                if len(words) >= 2:
                    return " ".join(
                        words[1:]
                    )

        return ""

    def passport_ocr(jpegs):
        """
        У PaddleOCR нет специального российского
        passport API, поэтому сначала распознаем
        весь текст, затем извлекаем основные поля.
        """
        # Проверяем доступность OCR
        if not ocr_configured_local():
            return {}

        text = general_ocr(
            jpegs
        )

        lines = [
            _clean_ocr_line(line)
            for line in text.splitlines()
            if _clean_ocr_line(line)
        ]

        entities = {
            "_raw_text": text
        }

        surname = _value_after_label(
            lines,
            "фамилия",
        )

        first_name = _value_after_label(
            lines,
            "имя",
        )

        middle_name = _value_after_label(
            lines,
            "отчество",
        )

        if surname:
            entities[
                "surname"
            ] = surname

        if first_name:
            entities[
                "name"
            ] = first_name

        if middle_name:
            entities[
                "middle_name"
            ] = middle_name

        # ------------------------------
        # Дата рождения
        # ------------------------------

        birth_value = (
            _value_after_label(
                lines,
                "дата рождения",
            )
        )

        birth_match = re.search(
            r"\b\d{2}[./-]"
            r"\d{2}[./-]"
            r"\d{4}\b",
            birth_value,
        )

        if not birth_match:

            full_birth_match = re.search(
                r"(?:дата\s+рождения)"
                r"[^\d]{0,30}"
                r"(\d{2}[./-]"
                r"\d{2}[./-]"
                r"\d{4})",
                text,
                flags=re.IGNORECASE,
            )

            if full_birth_match:
                birth_match = (
                    full_birth_match
                )

                entities[
                    "birth_date"
                ] = (
                    full_birth_match
                    .group(1)
                )

        elif birth_match:
            entities[
                "birth_date"
            ] = birth_match.group(0)

        # ------------------------------
        # Серия + номер паспорта
        # ------------------------------

        compact_text = " ".join(
            text.split()
        )

        patterns = [
            # 45 01 123456
            r"\b(\d{2})\s+"
            r"(\d{2})\s+"
            r"(\d{6})\b",

            # 4501 123456
            r"\b(\d{4})\s+"
            r"(\d{6})\b",

            # 4501123456
            r"\b(\d{10})\b",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                compact_text,
            )

            if not match:
                continue

            number = "".join(
                match.groups()
            )

            number = re.sub(
                r"\D",
                "",
                number,
            )

            if len(number) == 10:
                entities[
                    "number"
                ] = number
                break

        # ------------------------------
        # Пол
        # ------------------------------

        normalized = (
            _normalize_ocr_label(
                text
            )
        )

        if re.search(
            r"\bжен\b|\bженский\b",
            normalized,
        ):
            entities[
                "gender"
            ] = "Женский"

        elif re.search(
            r"\bмуж\b|\bмужской\b",
            normalized,
        ):
            entities[
                "gender"
            ] = "Мужской"

        return entities

    # ========================================================
    # Нормализация (без изменений)
    # ========================================================

    def normalized_text(value: str) -> str:
        value = str(
            value or ""
        ).lower().replace(
            "ё",
            "е",
        )

        return re.sub(
            r"[^a-zа-я0-9]+",
            " ",
            value,
        ).strip()

    def normalized_person(value: str) -> str:
        return normalized_text(value)

    def digits(value: str) -> str:
        return re.sub(
            r"\D",
            "",
            value or "",
        )

    def normalize_gender(
        value: str,
    ) -> str:
        value = normalized_text(value)

        if value in {
            "жен",
            "женский",
            "female",
            "f",
        }:
            return "Женский"

        if value in {
            "муж",
            "мужской",
            "male",
            "m",
        }:
            return "Мужской"

        return ""

    def date_to_iso(
        value: str,
    ) -> str:
        match = re.search(
            r"(\d{2})[./-]"
            r"(\d{2})[./-]"
            r"(\d{4})",
            value or "",
        )

        if not match:
            return ""

        day, month, year = (
            match.groups()
        )

        try:
            parsed = datetime.strptime(
                f"{day}.{month}.{year}",
                "%d.%m.%Y",
            )

        except ValueError:
            return ""

        return parsed.strftime(
            "%Y-%m-%d"
        )

    def format_snils(
        value: str,
    ) -> str:
        number = digits(value)

        if len(number) != 11:
            return ""

        return (
            f"{number[:3]}-"
            f"{number[3:6]}-"
            f"{number[6:9]} "
            f"{number[9:]}"
        )

    # ========================================================
    # SNILS OCR (без изменений)
    # ========================================================

    def _snils_checksum_state(
        value: str,
    ):
        """
        True  = контрольное число совпало.
        False = контрольное число не совпало.
        None  = номер нельзя проверить.
        """

        number = re.sub(
            r"\D",
            "",
            value or "",
        )

        if len(number) != 11:
            return None

        first_nine = number[:9]

        try:
            base_number = int(first_nine)
        except ValueError:
            return None

        # Для самых ранних номеров
        # контрольная сумма исторически
        # не является надежным признаком.
        if base_number <= 1001998:
            return None

        total = sum(
            int(digit) * weight
            for digit, weight in zip(
                first_nine,
                range(9, 0, -1),
            )
        )

        if total < 100:
            expected = total

        elif total in (100, 101):
            expected = 0

        else:
            expected = total % 101

            if expected == 100:
                expected = 0

        actual = int(
            number[-2:]
        )

        return actual == expected

    def _snils_numbers_from_text(
        text: str,
    ):
        """
        Ищет номера вида:
        123-456-789 01
        123 456 789 01
        12345678901
        """

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

        results = []

        for match in pattern.finditer(
            text or ""
        ):
            number = "".join(
                match.groups()
            )

            formatted = (
                f"{number[:3]}-"
                f"{number[3:6]}-"
                f"{number[6:9]} "
                f"{number[9:]}"
            )

            if formatted not in results:
                results.append(
                    formatted
                )

        return results

    def _snils_text_analysis(
        text: str,
    ):
        normalized = normalized_text(
            text
        )

        numbers = (
            _snils_numbers_from_text(
                text
            )
        )

        valid_number = None

        for number in numbers:

            if (
                _snils_checksum_state(
                    number
                )
                is True
            ):
                valid_number = number
                break

        strong_phrases = (
            "страховое свидетельство",
            (
                "обязательного "
                "пенсионного страхования"
            ),
            (
                "страховой номер "
                "индивидуального "
                "лицевого счета"
            ),
        )

        other_phrases = (
            "снилс",
            "страховой номер",
            "лицевого счета",
            "пенсионного страхования",
            "страхования",
        )

        strong_hits = sum(
            phrase in normalized
            for phrase in strong_phrases
        )

        keyword_hits = sum(
            phrase in normalized
            for phrase in other_phrases
        )

        # Самый надежный вариант:
        # корректный СНИЛС + название документа.
        if (
            valid_number
            and (
                strong_hits > 0
                or keyword_hits > 0
            )
        ):
            return {
                "detected": "snils",
                "confidence": 0.99,
                "snils": valid_number,
                "rank": 1000,
            }

        # Корректное контрольное число само
        # по себе — очень сильный признак.
        if valid_number:
            return {
                "detected": "snils",
                "confidence": 0.90,
                "snils": valid_number,
                "rank": 900,
            }

        # Заголовок + номер есть,
        # но OCR мог ошибиться в одной цифре.
        if (
            numbers
            and (
                strong_hits > 0
                or keyword_hits >= 2
            )
        ):
            return {
                "detected": "unknown",
                "confidence": 0.68,
                "snils": numbers[0],
                "rank": 700,
            }

        if strong_hits > 0:
            return {
                "detected": "unknown",
                "confidence": 0.58,
                "snils": "",
                "rank": 580,
            }

        if keyword_hits >= 2:
            return {
                "detected": "unknown",
                "confidence": 0.50,
                "snils": "",
                "rank": 500,
            }

        return {
            "detected": "unknown",
            "confidence": 0.0,
            "snils": "",
            "rank": 0,
        }

    def _crop_document_region(
        image,
    ):
        """
        Убирает большую часть белого поля
        вокруг отсканированного документа.
        """

        import numpy as np

        image = ImageOps.exif_transpose(
            image
        ).convert("RGB")

        width, height = image.size

        # Сразу убираем край сканера,
        # который часто выглядит как
        # черная рамка.
        edge_x = max(
            2,
            int(width * 0.012),
        )

        edge_y = max(
            2,
            int(height * 0.012),
        )

        if (
            width > edge_x * 2
            and height > edge_y * 2
        ):
            working = image.crop(
                (
                    edge_x,
                    edge_y,
                    width - edge_x,
                    height - edge_y,
                )
            )
        else:
            working = image

        gray = np.asarray(
            working.convert("L")
        )

        # Белый фон обычно близок к 255.
        # Зеленая карточка и текст существенно
        # темнее.
        dark = gray < 243

        row_counts = dark.sum(
            axis=1
        )

        column_counts = dark.sum(
            axis=0
        )

        row_threshold = max(
            6,
            int(
                working.width
                * 0.005
            ),
        )

        column_threshold = max(
            6,
            int(
                working.height
                * 0.005
            ),
        )

        rows = np.where(
            row_counts
            >= row_threshold
        )[0]

        columns = np.where(
            column_counts
            >= column_threshold
        )[0]

        if (
            len(rows) == 0
            or len(columns) == 0
        ):
            return image

        left = int(columns[0])
        right = int(columns[-1]) + 1

        top = int(rows[0])
        bottom = int(rows[-1]) + 1

        content_width = (
            right - left
        )

        content_height = (
            bottom - top
        )

        # Защита от случайной точки/пыли.
        content_area = (
            content_width
            * content_height
        )

        if (
            content_area
            < working.width
            * working.height
            * 0.02
        ):
            return image

        margin_x = max(
            12,
            int(
                content_width
                * 0.05
            ),
        )

        margin_y = max(
            12,
            int(
                content_height
                * 0.05
            ),
        )

        left = max(
            0,
            left - margin_x,
        )

        right = min(
            working.width,
            right + margin_x,
        )

        top = max(
            0,
            top - margin_y,
        )

        bottom = min(
            working.height,
            bottom + margin_y,
        )

        return working.crop(
            (
                left,
                top,
                right,
                bottom,
            )
        )

    def _image_to_jpeg_bytes_v64(
        image,
    ):
        image = ImageOps.exif_transpose(
            image
        ).convert("RGB")

        # После обрезки маленький документ
        # немного увеличиваем перед OCR.
        maximum = max(
            image.size
        )

        if maximum < 1400:

            scale = min(
                2.0,
                1400.0 / maximum,
            )

            image = image.resize(
                (
                    max(
                        1,
                        int(
                            image.width
                            * scale
                        ),
                    ),
                    max(
                        1,
                        int(
                            image.height
                            * scale
                        ),
                    ),
                ),
                Image.Resampling.LANCZOS,
            )

        output = io.BytesIO()

        image.save(
            output,
            format="JPEG",
            quality=94,
        )

        return output.getvalue()

    def _snils_image_variants(
        jpeg: bytes,
    ):
        """
        Сначала вырезаем область документа,
        затем пробуем четыре ориентации.
        """

        image = Image.open(
            io.BytesIO(jpeg)
        )

        image = ImageOps.exif_transpose(
            image
        ).convert("RGB")

        cropped = _crop_document_region(
            image
        )

        variants = []

        # Наиболее частые варианты
        # ставим первыми.
        for angle in (
            0,
            90,
            270,
            180,
        ):

            if angle == 0:
                rotated = cropped
            else:
                rotated = cropped.rotate(
                    angle,
                    expand=True,
                    fillcolor="white",
                )

            variants.append(
                (
                    f"crop_{angle}",
                    _image_to_jpeg_bytes_v64(
                        rotated
                    ),
                )
            )

        return variants

    def recognize_snils_document(
        jpegs,
    ):
        """
        Отдельный OCR-сценарий для СНИЛС.
        """
        # Проверяем доступность OCR
        if not ocr_configured_local():
            return {
                "text": "",
                "detected": "unknown",
                "confidence": 0.0,
                "snils": "",
                "rank": -1,
                "variant": "",
                "error": "OCR не доступен"
            }

        best = {
            "text": "",
            "detected": "unknown",
            "confidence": 0.0,
            "snils": "",
            "rank": -1,
            "variant": "",
        }

        errors = []

        for page_number, jpeg in enumerate(
            jpegs,
            start=1,
        ):

            for (
                variant_name,
                variant_jpeg,
            ) in _snils_image_variants(
                jpeg
            ):

                try:
                    text = (
                        _recognize_local_page(
                            variant_jpeg
                        )
                    )

                except Exception as error:
                    errors.append(
                        (
                            f"страница "
                            f"{page_number}, "
                            f"{variant_name}: "
                            f"{error!r}"
                        )
                    )

                    continue

                analysis = (
                    _snils_text_analysis(
                        text
                    )
                )

                if (
                    analysis["rank"]
                    > best["rank"]
                ):
                    best = {
                        **analysis,
                        "text": text,
                        "variant":
                            variant_name,
                    }

                # Уже найден надежный СНИЛС.
                # Остальные повороты не нужны.
                if (
                    analysis[
                        "confidence"
                    ]
                    >= 0.97
                ):
                    return best

        if best["rank"] >= 0:
            return best

        raise RuntimeError(
            "Не удалось обработать "
            "СНИЛС ни в одной ориентации. "
            + " | ".join(
                errors[-4:]
            )
        )

    def detect_document_type(
        text: str,
    ):
        normalized = normalized_text(
            text
        )

        candidates = []

        # СНИЛС
        snils_match = re.search(
            r"\b\d{3}[-\s]?"
            r"\d{3}[-\s]?"
            r"\d{3}\s?\d{2}\b",
            text,
        )

        if (
            snils_match
            and (
                "снилс" in normalized
                or "страхов" in normalized
                or "лицевого счета"
                in normalized
            )
        ):
            candidates.append(
                (
                    "snils",
                    0.98,
                )
            )

        # Свидетельство о рождении
        if (
            "свидетельство о рождении"
            in normalized
        ):
            candidates.append(
                (
                    "birth_certificate",
                    0.98,
                )
            )

        # Заявление на прикрепление
        attach_score = sum(
            phrase in normalized
            for phrase in (
                "прошу прикрепить",
                "промежуточной аттестации",
                "в качестве экстерна",
                "семейное образование",
            )
        )

        if attach_score >= 3:
            candidates.append(
                (
                    "attachment_application",
                    0.96,
                )
            )

        elif attach_score == 2:
            candidates.append(
                (
                    "attachment_application",
                    0.70,
                )
            )

        # Уведомление
        notice_score = sum(
            phrase in normalized
            for phrase in (
                "уведомление",
                "о выборе формы получения образования",
                "семейного образования",
                "части 5 статьи 63",
            )
        )

        if notice_score >= 3:
            candidates.append(
                (
                    "education_notice",
                    0.97,
                )
            )

        elif notice_score == 2:
            candidates.append(
                (
                    "education_notice",
                    0.72,
                )
            )

        # Согласие родителя
        consent_score = sum(
            phrase in normalized
            for phrase in (
                "согласие законного представителя",
                "обработку персональных данных",
                "оператор",
                "отзыва согласия",
            )
        )

        if consent_score >= 3:
            candidates.append(
                (
                    "parent_consent",
                    0.97,
                )
            )

        elif consent_score == 2:
            candidates.append(
                (
                    "parent_consent",
                    0.72,
                )
            )

        # Заявление на отчисление
        if (
            "заявлен" in normalized
            and (
                "отчисл" in normalized
                or "прекращ" in normalized
            )
        ):
            candidates.append(
                (
                    "withdrawal_application",
                    0.90,
                )
            )

        # Паспорт через общий OCR.
        passport_score = sum(
            phrase in normalized
            for phrase in (
                "российская федерация",
                "паспорт",
                "код подразделения",
                "кем выдан",
            )
        )

        if passport_score >= 3:
            candidates.append(
                (
                    "passport",
                    0.90,
                )
            )

        elif passport_score == 2:
            candidates.append(
                (
                    "passport",
                    0.72,
                )
            )

        if not candidates:
            return (
                "unknown",
                0.0,
            )

        candidates.sort(
            key=lambda item: item[1],
            reverse=True,
        )

        return candidates[0]

    # ========================================================
    # Остальные функции (без изменений) - сокращенно
    # ========================================================

    def passport_candidates(
        expected_type: str,
        entities: dict,
    ) -> dict[str, str]:

        number = digits(
            entities.get(
                "number",
                "",
            )
        )

        series = (
            number[:4]
            if len(number) >= 10
            else ""
        )

        passport_number = (
            number[-6:]
            if len(number) >= 10
            else ""
        )

        gender = normalize_gender(
            entities.get(
                "gender",
                "",
            )
        )

        birth_date = date_to_iso(
            entities.get(
                "birth_date",
                "",
            )
        )

        if expected_type == "parent_passport":
            return {
                "parent_gender":
                    gender,

                "parent_birth_date":
                    birth_date,

                "parent_passport_series":
                    series,

                "parent_passport_number":
                    passport_number,
            }

        return {
            "gender":
                gender,

            "child_passport_series":
                series,

            "child_passport_number":
                passport_number,
        }

    def extract_snils(
        text: str,
    ) -> str:

        numbers = (
            _snils_numbers_from_text(
                text
            )
        )

        # Сначала выбираем номер
        # с корректным контрольным числом.
        for number in numbers:

            if (
                _snils_checksum_state(
                    number
                )
                is True
            ):
                return number

        # Если OCR мог ошибиться
        # в одной цифре, возвращаем номер
        # только для ручной проверки.
        if numbers:
            return numbers[0]

        return ""

    # ========================================================
    # BIRTH CERTIFICATE OCR (без изменений)
    # ========================================================

    BIRTH_DAY_WORDS = {
        1: "первого",
        2: "второго",
        3: "третьего",
        4: "четвертого",
        5: "пятого",
        6: "шестого",
        7: "седьмого",
        8: "восьмого",
        9: "девятого",
        10: "десятого",
        11: "одиннадцатого",
        12: "двенадцатого",
        13: "тринадцатого",
        14: "четырнадцатого",
        15: "пятнадцатого",
        16: "шестнадцатого",
        17: "семнадцатого",
        18: "восемнадцатого",
        19: "девятнадцатого",
        20: "двадцатого",
        21: "двадцать первого",
        22: "двадцать второго",
        23: "двадцать третьего",
        24: "двадцать четвертого",
        25: "двадцать пятого",
        26: "двадцать шестого",
        27: "двадцать седьмого",
        28: "двадцать восьмого",
        29: "двадцать девятого",
        30: "тридцатого",
        31: "тридцать первого",
    }

    BIRTH_MONTH_WORDS = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }

    def _birth_ocr_date_text(
        text: str,
    ) -> str:
        """
        Исправляет наиболее частые
        OCR-подмены внутри цифровых дат.
        """

        value = str(
            text or ""
        )

        value = value.replace(
            "О",
            "0",
        ).replace(
            "O",
            "0",
        )

        value = value.replace(
            "I",
            "1",
        ).replace(
            "l",
            "1",
        ).replace(
            "|",
            "1",
        )

        # Частая OCR-ошибка:
        # 201B вместо 2018.
        value = re.sub(
            r"(?<=\d)[BВ](?=\D|$)",
            "8",
            value,
        )

        return value

    def _birth_date_candidates(
        text: str,
    ):
        """
        Возвращает все корректные
        цифровые даты, найденные OCR.
        """

        value = _birth_ocr_date_text(
            text
        )

        pattern = re.compile(
            r"(?<!\d)"
            r"([0-3]?\d)"
            r"\s*[./\-]\s*"
            r"([01]?\d)"
            r"\s*[./\-]\s*"
            r"((?:19|20)\d{2})"
            r"(?!\d)"
        )

        result = []

        for match in pattern.finditer(
            value
        ):

            day = int(
                match.group(1)
            )

            month = int(
                match.group(2)
            )

            year = int(
                match.group(3)
            )

            try:
                parsed = datetime(
                    year,
                    month,
                    day,
                ).date()

            except ValueError:
                continue

            iso = parsed.isoformat()

            if iso not in result:
                result.append(
                    iso
                )

        return result

    def birth_date_check(
        text: str,
        expected_birth_date: str,
    ):
        """
        Дату рождения подтверждаем
        двумя способами.
        """

        if not expected_birth_date:
            return {
                "confirmed": False,
                "recognized": "",
                "method": "",
            }

        try:
            expected = datetime.strptime(
                expected_birth_date,
                "%Y-%m-%d",
            ).date()

        except ValueError:
            return {
                "confirmed": False,
                "recognized": "",
                "method": "",
            }

        # -----------------------------
        # 1. Цифровая дата
        # -----------------------------

        dates = _birth_date_candidates(
            text
        )

        if expected.isoformat() in dates:
            return {
                "confirmed": True,
                "recognized":
                    expected.strftime(
                        "%d.%m.%Y"
                    ),
                "method": "numeric",
            }

        # -----------------------------
        # 2. Дата словами
        # -----------------------------

        normalized = normalized_text(
            text
        )

        day_phrase = BIRTH_DAY_WORDS.get(
            expected.day,
            "",
        )

        month_phrase = (
            BIRTH_MONTH_WORDS.get(
                expected.month,
                "",
            )
        )

        phrase_confirmed = (
            day_phrase
            and month_phrase
            and day_phrase in normalized
            and month_phrase in normalized
        )

        if phrase_confirmed:
            return {
                "confirmed": True,
                "recognized":
                    expected.strftime(
                        "%d.%m.%Y"
                    ),
                "method": "words",
            }

        return {
            "confirmed": False,
            "recognized": "",
            "method": "",
            "found_dates": dates,
        }

    def _normalize_birth_series(
        roman: str,
        letters: str,
    ) -> str:

        roman = (
            roman.upper()
            .replace("1", "I")
        )

        letters = letters.upper()

        # OCR иногда распознает
        # кириллицу латиницей.
        translation = str.maketrans(
            {
                "T": "Т",
                "H": "Н",
                "N": "Н",
                "K": "К",
                "M": "М",
                "C": "С",
                "B": "В",
                "P": "Р",
                "A": "А",
                "E": "Е",
                "O": "О",
                "X": "Х",
            }
        )

        letters = letters.translate(
            translation
        )

        return (
            roman
            + "-"
            + letters
        )

    def birth_certificate_requisites(
        text: str,
    ):
        """
        Извлекает серию и номер.
        """

        value = str(
            text or ""
        ).upper()

        value = (
            value.replace("–", "-")
            .replace("—", "-")
        )

        patterns = [
            re.compile(
                r"(?<![A-ZА-ЯЁ0-9])"
                r"([IVXLCDM1]{1,6})"
                r"\s*-\s*"
                r"([A-ZА-ЯЁ]{2})"
                r"\s*"
                r"(?:№|N|NO|#)?"
                r"\s*"
                r"(\d{3})\s*(\d{3})"
                r"(?!\d)"
            ),

            re.compile(
                r"(?<![A-ZА-ЯЁ0-9])"
                r"([IVXLCDM1]{1,6})"
                r"\s+"
                r"([A-ZА-ЯЁ]{2})"
                r"\s*"
                r"(?:№|N|NO|#)?"
                r"\s*"
                r"(\d{3})\s*(\d{3})"
                r"(?!\d)"
            ),
        ]

        for pattern in patterns:

            match = pattern.search(
                value
            )

            if not match:
                continue

            series = (
                _normalize_birth_series(
                    match.group(1),
                    match.group(2),
                )
            )

            number = (
                match.group(3)
                + match.group(4)
            )

            return {
                "series": series,
                "number": number,
            }

        return {
            "series": "",
            "number": "",
        }

    def birth_certificate_candidates(
        text: str,
    ):

        requisites = (
            birth_certificate_requisites(
                text
            )
        )

        candidates = {}

        if requisites["series"]:
            candidates[
                "birth_certificate_series"
            ] = requisites[
                "series"
            ]

        if requisites["number"]:
            candidates[
                "birth_certificate_number"
            ] = requisites[
                "number"
            ]

        return candidates

    # ========================================================
    # Проверка принадлежности (без изменений)
    # ========================================================

    def passport_identity_checks(
        student,
        expected_type,
        entities,
    ):
        checks = []
        status = "passed"

        surname = normalized_person(
            entities.get(
                "surname",
                "",
            )
        )

        first_name = normalized_person(
            entities.get(
                "name",
                "",
            )
        )

        birth_date = date_to_iso(
            entities.get(
                "birth_date",
                "",
            )
        )

        if expected_type == "parent_passport":
            expected_surname = normalized_person(
                student[
                    "parent_last_name"
                ]
            )

            expected_first_name = normalized_person(
                student[
                    "parent_first_name"
                ]
            )

            expected_birth_date = (
                student[
                    "parent_birth_date"
                ]
                or ""
            )

            stored_number = digits(
                (
                    student[
                        "parent_passport_series"
                    ]
                    or ""
                )
                + (
                    student[
                        "parent_passport_number"
                    ]
                    or ""
                )
            )

        else:
            expected_surname = normalized_person(
                student[
                    "last_name"
                ]
            )

            expected_first_name = normalized_person(
                student[
                    "first_name"
                ]
            )

            expected_birth_date = (
                student[
                    "birth_date"
                ]
                or ""
            )

            stored_number = digits(
                (
                    student[
                        "child_passport_series"
                    ]
                    or ""
                )
                + (
                    student[
                        "child_passport_number"
                    ]
                    or ""
                )
            )

        if surname and first_name:
            if (
                surname != expected_surname
                or first_name
                != expected_first_name
            ):
                checks.append(
                    {
                        "level": "red",
                        "text":
                            "ФИО в паспорте "
                            "не совпадает с карточкой.",
                    }
                )

                status = "mismatch"

            else:
                checks.append(
                    {
                        "level": "green",
                        "text":
                            "ФИО совпадает "
                            "с карточкой.",
                    }
                )

        else:
            checks.append(
                {
                    "level": "yellow",
                    "text":
                        "Не удалось уверенно "
                        "распознать ФИО.",
                }
            )

            if status != "mismatch":
                status = "warning"

        if (
            birth_date
            and expected_birth_date
        ):
            if (
                birth_date
                != expected_birth_date
            ):
                checks.append(
                    {
                        "level": "red",
                        "text":
                            "Дата рождения "
                            "не совпадает.",
                    }
                )

                status = "mismatch"

            else:
                checks.append(
                    {
                        "level": "green",
                        "text":
                            "Дата рождения "
                            "совпадает.",
                    }
                )

        recognized_number = digits(
            entities.get(
                "number",
                "",
            )
        )

        if (
            stored_number
            and recognized_number
            and stored_number
            != recognized_number
        ):
            checks.append(
                {
                    "level": "red",
                    "text":
                        "Номер паспорта "
                        "не совпадает "
                        "с ранее указанным.",
                }
            )

            status = "mismatch"

        return status, checks

    def text_identity_checks(
        student,
        expected_type,
        text,
    ):
        checks = []
        status = "passed"

        normalized = normalized_text(
            text
        )

        child_surname = normalized_person(
            student["last_name"]
        )

        child_name = normalized_person(
            student["first_name"]
        )

        parent_surname = normalized_person(
            student[
                "parent_last_name"
            ]
        )

        parent_name = normalized_person(
            student[
                "parent_first_name"
            ]
        )

        if expected_type in {
            "birth_certificate",
            "attachment_application",
            "withdrawal_application",
            "parent_consent",
            "education_notice",
        }:
            child_found = (
                child_surname in normalized
                and child_name in normalized
            )

            if child_found:
                checks.append(
                    {
                        "level": "green",
                        "text":
                            "ФИО ребенка "
                            "найдено в документе.",
                    }
                )

            else:
                checks.append(
                    {
                        "level": "yellow",
                        "text":
                            "Не удалось уверенно "
                            "подтвердить ФИО ребенка.",
                    }
                )

                status = "warning"

        if expected_type in {
            "attachment_application",
            "withdrawal_application",
            "parent_consent",
            "education_notice",
        }:
            parent_found = (
                parent_surname in normalized
                and parent_name in normalized
            )

            if parent_found:
                checks.append(
                    {
                        "level": "green",
                        "text":
                            "ФИО законного "
                            "представителя найдено.",
                    }
                )

            else:
                checks.append(
                    {
                        "level": "yellow",
                        "text":
                            "Не удалось уверенно "
                            "подтвердить ФИО "
                            "законного представителя.",
                    }
                )

                status = "warning"

        if (
            expected_type
            == "birth_certificate"
        ):

            expected_birth_date = (
                student["birth_date"]
                or ""
            )

            date_result = (
                birth_date_check(
                    text,
                    expected_birth_date,
                )
            )

            if date_result[
                "confirmed"
            ]:

                checks.append(
                    {
                        "level": "green",
                        "text":
                            "Дата рождения "
                            "совпадает: "
                            + date_result[
                                "recognized"
                            ]
                            + ".",
                    }
                )

            else:

                checks.append(
                    {
                        "level": "yellow",
                        "text":
                            "Не удалось надежно "
                            "подтвердить дату "
                            "рождения по OCR.",
                    }
                )

                if status != "mismatch":
                    status = "warning"

            requisites = (
                birth_certificate_requisites(
                    text
                )
            )

            if (
                requisites["series"]
                and requisites["number"]
            ):

                checks.append(
                    {
                        "level": "green",
                        "text":
                            "Серия и номер "
                            "свидетельства: "
                            + requisites[
                                "series"
                            ]
                            + " № "
                            + requisites[
                                "number"
                            ]
                            + ".",
                    }
                )

            else:

                checks.append(
                    {
                        "level": "yellow",
                        "text":
                            "Серия и номер "
                            "свидетельства "
                            "распознаны "
                            "не полностью.",
                    }
                )

        return status, checks

    # ========================================================
    # Применение данных (без изменений)
    # ========================================================

    def apply_empty_fields(
        student_id: int,
        candidates: dict[str, str],
    ):
        student = get_db().execute(
            """
            SELECT *
            FROM students
            WHERE id = ?
            """,
            (student_id,),
        ).fetchone()

        fields = {}

        for field, value in candidates.items():
            if (
                not value
                or field not in student.keys()
            ):
                continue

            current = str(
                student[field] or ""
            ).strip()

            if not current:
                fields[field] = value

        if not fields:
            return []

        assignments = ", ".join(
            f"{field} = ?"
            for field in fields
        )

        get_db().execute(
            f"""
            UPDATE students
            SET
                {assignments},
                updated_at = ?
            WHERE id = ?
            """,
            [
                *fields.values(),
                datetime.now().isoformat(
                    timespec="seconds"
                ),
                student_id,
            ],
        )

        get_db().commit()

        return list(fields)

    # ========================================================
    # Главная проверка (обновленная)
    # ========================================================

    def validate_document(
        document,
        student,
    ):
        expected = document[
            "document_type"
        ]

        checks = []
        candidates = {}
        entities = {}
        text = ""

        if expected not in AUTO_VALIDATION_TYPES:
            return {
                "status": "warning",
                "detected": "unknown",
                "confidence": 0.0,
                "message":
                    "Для этого типа документа "
                    "предусмотрена ручная проверка.",
                "checks": [
                    {
                        "level": "yellow",
                        "text":
                            "Автоматическая "
                            "классификация этого "
                            "документа пока "
                            "не используется.",
                    }
                ],
                "candidates": {},
                "ocr_text": "",
                "entities": {},
                "ocr_status":
                    "not_applicable",
            }

        # Проверяем доступность OCR
        if not ocr_configured_local():
            return {
                "status": "warning",
                "detected": "unknown",
                "confidence": 0.0,
                "message":
                    "OCR не доступен в облачной среде. "
                    "Документ сохранен для ручной проверки.",
                "checks": [
                    {
                        "level": "yellow",
                        "text":
                            "PaddleOCR не доступен. "
                            "Проверьте установку библиотек "
                            "или настройки облачной среды.",
                    }
                ],
                "candidates": {},
                "ocr_text": "",
                "entities": {},
                "ocr_status":
                    "not_configured",
            }

        path = (
            upload_dir
            / document["stored_name"]
        )

        try:
            jpegs = document_to_jpegs(
                path
            )
        except Exception as e:
            return {
                "status": "warning",
                "detected": "unknown",
                "confidence": 0.0,
                "message":
                    f"Не удалось обработать файл: {str(e)}",
                "checks": [
                    {
                        "level": "yellow",
                        "text":
                            f"Ошибка обработки файла: {str(e)}",
                    }
                ],
                "candidates": {},
                "ocr_text": "",
                "entities": {},
                "ocr_status":
                    "error",
            }

        expected_family = (
            EXPECTED_FAMILY[expected]
        )

        detected = "unknown"
        confidence = 0.0

        # Паспорт сначала проверяем
        # специализированной моделью.
        if expected_family == "passport":
            try:
                entities = passport_ocr(
                    jpegs
                )
            except Exception as e:
                entities = {}

            if (
                entities.get("surname")
                and entities.get("number")
            ):
                detected = "passport"
                confidence = 0.99

            else:
                try:
                    text = general_ocr(
                        jpegs
                    )
                except Exception as e:
                    text = ""

                if text.strip():
                    (
                        detected,
                        confidence,
                    ) = detect_document_type(
                        text
                    )
                else:
                    detected = "unknown"
                    confidence = 0.0

        elif expected_family == "snils":

            try:
                snils_result = (
                    recognize_snils_document(
                        jpegs
                    )
                )

                text = snils_result[
                    "text"
                ]

                detected = snils_result[
                    "detected"
                ]

                confidence = float(
                    snils_result[
                        "confidence"
                    ]
                )
            except Exception as e:
                text = ""
                detected = "unknown"
                confidence = 0.0

        else:
            try:
                text = general_ocr(
                    jpegs
                )
            except Exception as e:
                text = ""

            if text.strip():
                (
                    detected,
                    confidence,
                ) = detect_document_type(
                    text
                )
            else:
                detected = "unknown"
                confidence = 0.0

        # Тип документа
        if detected == expected_family:
            checks.append(
                {
                    "level": "green",
                    "text":
                        "Тип документа "
                        "соответствует ожидаемому.",
                }
            )

        elif (
            detected != "unknown"
            and confidence >= 0.70
        ):
            checks.append(
                {
                    "level": "red",
                    "text":
                        "Загружен документ "
                        "другого типа.",
                }
            )

            return {
                "status": "mismatch",
                "detected": detected,
                "confidence": confidence,
                "message":
                    "Обнаружено несовпадение "
                    "типа документа. "
                    "Автозаполнение заблокировано.",
                "checks": checks,
                "candidates": {},
                "ocr_text": text,
                "entities": entities,
                "ocr_status": "done",
            }

        else:
            checks.append(
                {
                    "level": "yellow",
                    "text":
                        "Не удалось уверенно "
                        "определить тип документа.",
                }
            )

            return {
                "status": "warning",
                "detected": detected,
                "confidence": confidence,
                "message":
                    "Документ требует "
                    "ручной проверки.",
                "checks": checks,
                "candidates": {},
                "ocr_text": text,
                "entities": entities,
                "ocr_status": "done",
            }

        # Сверка данных
        identity_status = "passed"

        if expected_family == "passport":
            (
                identity_status,
                identity_checks,
            ) = passport_identity_checks(
                student,
                expected,
                entities,
            )

            checks.extend(
                identity_checks
            )

            candidates = passport_candidates(
                expected,
                entities,
            )

        elif expected_family == "snils":

            recognized_snils = (
                extract_snils(
                    text
                )
            )

            if not recognized_snils:

                checks.append(
                    {
                        "level": "yellow",
                        "text":
                            "Не удалось надежно "
                            "распознать номер СНИЛС.",
                    }
                )

                identity_status = "warning"

            else:

                checksum_state = (
                    _snils_checksum_state(
                        recognized_snils
                    )
                )

                if checksum_state is False:

                    checks.append(
                        {
                            "level": "yellow",
                            "text":
                                "Номер похож на СНИЛС, "
                                "но контрольное число "
                                "не совпало. Возможно, "
                                "OCR ошибся в одной "
                                "из цифр.",
                        }
                    )

                    identity_status = "warning"

                else:

                    checks.append(
                        {
                            "level": "green",
                            "text":
                                "Номер СНИЛС "
                                "распознан.",
                        }
                    )

                    if checksum_state is True:

                        checks.append(
                            {
                                "level": "green",
                                "text":
                                    "Контрольное число "
                                    "СНИЛС корректно.",
                            }
                        )

                    field = (
                        "parent_snils"
                        if expected
                        == "parent_snils"
                        else "student_snils"
                    )

                    stored = digits(
                        student[field]
                        or ""
                    )

                    recognized = digits(
                        recognized_snils
                    )

                    if (
                        stored
                        and stored
                        != recognized
                    ):

                        checks.append(
                            {
                                "level": "red",
                                "text":
                                    "Номер СНИЛС "
                                    "не совпадает "
                                    "с карточкой.",
                            }
                        )

                        identity_status = (
                            "mismatch"
                        )

                    else:

                        if stored:

                            checks.append(
                                {
                                    "level":
                                        "green",

                                    "text":
                                        "Номер СНИЛС "
                                        "совпадает "
                                        "с карточкой.",
                                }
                            )

                        candidates = {
                            field:
                                recognized_snils
                        }

        else:
            (
                identity_status,
                identity_checks,
            ) = text_identity_checks(
                student,
                expected,
                text,
            )

            checks.extend(
                identity_checks
            )

            if (
                expected
                == "birth_certificate"
            ):
                candidates = (
                    birth_certificate_candidates(
                        text
                    )
                )

        if identity_status == "mismatch":
            return {
                "status": "mismatch",
                "detected": detected,
                "confidence": confidence,
                "message":
                    "Тип документа верный, "
                    "но обнаружено расхождение "
                    "с карточкой. "
                    "Автозаполнение заблокировано.",
                "checks": checks,
                "candidates": {},
                "ocr_text": text,
                "entities": entities,
                "ocr_status": "done",
            }

        if identity_status == "warning":
            return {
                "status": "warning",
                "detected": detected,
                "confidence": confidence,
                "message":
                    "Тип документа похож "
                    "на ожидаемый, но часть "
                    "данных требует проверки.",
                "checks": checks,
                "candidates": {},
                "ocr_text": text,
                "entities": entities,
                "ocr_status": "done",
            }

        return {
            "status": "passed",
            "detected": detected,
            "confidence": confidence,
            "message":
                "Документ прошел "
                "предварительную проверку.",
            "checks": checks,
            "candidates": candidates,
            "ocr_text": text,
            "entities": entities,
            "ocr_status": "done",
        }

    # ========================================================
    # Остальные функции (без изменений)
    # ========================================================

    def document_to_jpegs(
        path: Path,
    ) -> list[bytes]:

        content = path.read_bytes()

        if b"%PDF" in content[:1024]:
            document = fitz.open(path)

            result = []

            try:
                page_limit = min(
                    len(document),
                    MAX_OCR_PAGES,
                )

                for number in range(
                    page_limit
                ):
                    page = document.load_page(
                        number
                    )

                    pixmap = page.get_pixmap(
                        matrix=fitz.Matrix(
                            1.7,
                            1.7,
                        ),
                        alpha=False,
                    )

                    result.append(
                        pixmap.tobytes(
                            "jpeg"
                        )
                    )

            finally:
                document.close()

            return result

        image = Image.open(
            io.BytesIO(content)
        )

        image = ImageOps.exif_transpose(
            image
        )

        if image.mode != "RGB":
            image = image.convert("RGB")

        output = io.BytesIO()

        image.save(
            output,
            format="JPEG",
            quality=90,
        )

        return [output.getvalue()]

    def run_validation(
        document_id: int,
    ):
        document = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

        if not document:
            raise RuntimeError(
                "Документ не найден."
            )

        student = get_student_or_404(
            document["student_id"]
        )

        try:
            result = validate_document(
                document,
                student,
            )

        except Exception as error:
            result = {
                "status": "warning",
                "detected": "unknown",
                "confidence": 0.0,
                "message":
                    "Автоматическая проверка "
                    "не завершена из-за "
                    "технической ошибки.",
                "checks": [
                    {
                        "level": "yellow",
                        "text":
                            f"Ошибка OCR: {error}",
                    }
                ],
                "candidates": {},
                "ocr_text": "",
                "entities": {},
                "ocr_status": "error",
            }

        # V7: На этапе проверки данные еще
        # НЕ записываем в карточку.
        autofilled = []

        blocked = (
            1
            if result["status"]
            == "mismatch"
            else 0
        )

        details = {
            "checks":
                result["checks"],

            "autofilled":
                autofilled,

            "expected":
                document["document_type"],

            "candidates":
                result.get(
                    "candidates",
                    {},
                ),
        }

        ocr_payload = {
            "entities":
                result.get(
                    "entities",
                    {},
                )
        }

        get_db().execute(
            """
            UPDATE documents
            SET
                ocr_status = ?,
                ocr_text = ?,
                ocr_json = ?,
                validation_status = ?,
                detected_document_type = ?,
                validation_confidence = ?,
                validation_message = ?,
                validation_details = ?,
                validated_at = ?,
                autofill_applied = ?,
                autofill_blocked = ?
            WHERE id = ?
            """,
            (
                result[
                    "ocr_status"
                ],
                result.get(
                    "ocr_text",
                    "",
                )[:100000],
                json.dumps(
                    ocr_payload,
                    ensure_ascii=False,
                ),
                result["status"],
                result["detected"],
                float(
                    result["confidence"]
                ),
                result["message"],
                json.dumps(
                    details,
                    ensure_ascii=False,
                ),
                datetime.now().isoformat(
                    timespec="seconds"
                ),
                1 if autofilled else 0,
                blocked,
                document_id,
            ),
        )

        get_db().commit()

        audit(
            "document_validated",
            student["id"],
            (
                f"document_id={document_id}; "
                f"expected="
                f"{document['document_type']}; "
                f"detected="
                f"{result['detected']}; "
                f"status="
                f"{result['status']}; "
                f"confidence="
                f"{result['confidence']:.2f}"
            ),
        )

        return result

    # ========================================================
    # V7 SAVE AFTER VALIDATION (без изменений)
    # ========================================================

    def migrate_save_workflow_v7():
        connection = sqlite3.connect(
            database_path
        )

        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }

            save_column_added = (
                "save_confirmed"
                not in columns
            )

            migrations = {
                "save_confirmed":
                    "ALTER TABLE documents "
                    "ADD COLUMN save_confirmed "
                    "INTEGER NOT NULL DEFAULT 0",

                "manual_review_required":
                    "ALTER TABLE documents "
                    "ADD COLUMN manual_review_required "
                    "INTEGER NOT NULL DEFAULT 0",

                "manual_review_reason":
                    "ALTER TABLE documents "
                    "ADD COLUMN manual_review_reason TEXT",

                "saved_at":
                    "ALTER TABLE documents "
                    "ADD COLUMN saved_at TEXT",

                "saved_by":
                    "ALTER TABLE documents "
                    "ADD COLUMN saved_by INTEGER",
            }

            for column, sql in migrations.items():
                if column not in columns:
                    connection.execute(sql)

            # Все документы, которые существовали
            # ДО установки v7, считаем уже сохраненными.
            if save_column_added:
                connection.execute(
                    """
                    UPDATE documents
                    SET
                        save_confirmed = 1,
                        saved_at = uploaded_at,
                        saved_by = uploaded_by
                    """
                )

            connection.commit()

        finally:
            connection.close()

    migrate_save_workflow_v7()

    def save_validated_document_v7(
        document_id: int,
    ):
        document = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

        if not document:
            abort(404)

        student = get_student_or_404(
            document["student_id"]
        )

        if (
            g.current_user["role"] == "branch"
            and student["branch_id"]
            != g.current_user["branch_id"]
        ):
            abort(403)

        if student["status"] not in (
            "draft",
            "correction",
        ):
            abort(
                400,
                "Карточка заблокирована "
                "для редактирования.",
            )

        validation_status = (
            document["validation_status"]
            or ""
        )

        if validation_status not in {
            "passed",
            "warning",
            "mismatch",
        }:
            flash(
                "Сначала необходимо завершить "
                "проверку документа.",
                "error",
            )

            return redirect(
                url_for(
                    "validation_result_v6",
                    document_id=document_id,
                )
            )

        if document["save_confirmed"]:
            flash(
                "Документ уже сохранен.",
                "info",
            )

            return redirect(
                url_for(
                    "student_detail",
                    student_id=student["id"],
                )
            )

        try:
            details = json.loads(
                document[
                    "validation_details"
                ]
                or "{}"
            )

        except (
            json.JSONDecodeError,
            TypeError,
        ):
            details = {}

        autofilled = []

        manual_review_required = 0
        manual_review_reason = None

        # ЗЕЛЕНЫЙ
        if validation_status == "passed":

            candidates = details.get(
                "candidates",
                {},
            )

            if isinstance(
                candidates,
                dict,
            ):
                autofilled = apply_empty_fields(
                    student["id"],
                    candidates,
                )

        # ЖЕЛТЫЙ
        elif validation_status == "warning":

            manual_review_required = 1

            manual_review_reason = (
                document[
                    "validation_message"
                ]
                or
                "Автоматическая проверка "
                "не дала однозначного результата."
            )

        # КРАСНЫЙ
        elif validation_status == "mismatch":

            manual_review_required = 1

            manual_review_reason = (
                document[
                    "validation_message"
                ]
                or
                "Автоматическая проверка "
                "обнаружила несовпадение."
            )

        details[
            "autofilled"
        ] = autofilled

        details[
            "save_confirmed"
        ] = True

        details[
            "manual_review_required"
        ] = bool(
            manual_review_required
        )

        get_db().execute(
            """
            UPDATE documents
            SET
                save_confirmed = 1,
                saved_at = ?,
                saved_by = ?,
                manual_review_required = ?,
                manual_review_reason = ?,
                autofill_applied = ?,
                validation_details = ?
            WHERE id = ?
            """,
            (
                datetime.now().isoformat(
                    timespec="seconds"
                ),
                g.current_user["id"],
                manual_review_required,
                manual_review_reason,
                1 if autofilled else 0,
                json.dumps(
                    details,
                    ensure_ascii=False,
                ),
                document_id,
            ),
        )

        get_db().execute(
            """
            UPDATE students
            SET updated_at = ?
            WHERE id = ?
            """,
            (
                datetime.now().isoformat(
                    timespec="seconds"
                ),
                student["id"],
            ),
        )

        get_db().commit()

        audit(
            "document_saved_after_validation",
            student["id"],
            (
                f"document_id={document_id}; "
                f"validation_status="
                f"{validation_status}; "
                f"manual_review="
                f"{manual_review_required}; "
                f"autofilled="
                f"{','.join(autofilled)}"
            ),
        )

        if validation_status == "passed":

            if autofilled:
                flash(
                    "Документ сохранен. "
                    "Проверка пройдена, "
                    "пустые поля карточки "
                    "заполнены автоматически.",
                    "success",
                )

            else:
                flash(
                    "Документ сохранен. "
                    "Проверка пройдена.",
                    "success",
                )

        elif validation_status == "warning":

            flash(
                "Документ сохранен. "
                "Он отмечен для ручной проверки. "
                "Автозаполнение не выполнялось.",
                "info",
            )

        else:

            flash(
                "Документ сохранен, несмотря "
                "на обнаруженное несовпадение. "
                "Он передан на ручную проверку. "
                "Автозаполнение заблокировано.",
                "error",
            )

        return redirect(
            url_for(
                "student_detail",
                student_id=student["id"],
            )
        )

    if (
        "save_validated_document_v7"
        not in app.view_functions
    ):
        app.add_url_rule(
            "/documents/"
            "<int:document_id>/save",
            endpoint=
                "save_validated_document_v7",
            view_func=roles_required(
                "branch"
            )(
                save_validated_document_v7
            ),
            methods=["POST"],
        )

    # ========================================================
    # Загрузка v6 (обновленная)
    # ========================================================

    def upload_document_v6(
        student_id: int,
        document_type: str,
    ):
        student = get_student_or_404(
            student_id
        )

        if student["status"] not in (
            "draft",
            "correction",
        ):
            abort(
                400,
                "Карточка заблокирована "
                "для редактирования.",
            )

        if document_type not in document_types:
            abort(404)

        existing_documents = (
            get_db().execute(
                """
                SELECT *
                FROM documents
                WHERE student_id = ?
                ORDER BY uploaded_at DESC
                """,
                (student_id,),
            ).fetchall()
        )

        # V6.5: ранее загруженные файлы повторно выбирать нельзя.
        existing_documents = []

        if request.method == "POST":
            reuse_id = ""

            source_document_id = None
            page_count = 1

            if reuse_id:
                source = get_db().execute(
                    """
                    SELECT *
                    FROM documents
                    WHERE id = ?
                      AND student_id = ?
                    """,
                    (
                        int(reuse_id),
                        student_id,
                    ),
                ).fetchone()

                if not source:
                    abort(
                        400,
                        "Исходный документ "
                        "не найден.",
                    )

                original_name = source[
                    "original_name"
                ]

                stored_name = source[
                    "stored_name"
                ]

                mime_type = source[
                    "mime_type"
                ]

                source_document_id = (
                    source["id"]
                )

                if (
                    "page_count"
                    in source.keys()
                ):
                    page_count = (
                        source[
                            "page_count"
                        ]
                        or 1
                    )

            else:
                uploads = [
                    item
                    for item
                    in request.files.getlist(
                        "files"
                    )
                    if (
                        item
                        and item.filename
                    )
                ]

                if not uploads:
                    flash(
                        "Выберите один или "
                        "несколько файлов.",
                        "error",
                    )

                    return redirect(
                        request.url
                    )

                try:
                    (
                        merged_pdf,
                        names,
                        page_count,
                    ) = merge_files(
                        uploads
                    )

                except ValueError as error:
                    flash(
                        str(error),
                        "error",
                    )

                    return redirect(
                        request.url
                    )

                stored_name = (
                    f"{uuid.uuid4().hex}.pdf"
                )

                (
                    upload_dir
                    / stored_name
                ).write_bytes(
                    merged_pdf
                )

                original_name = (
                    " + ".join(names)
                )[:1500]

                mime_type = (
                    "application/pdf"
                )

            latest_version = (
                get_db().execute(
                    """
                    SELECT
                        COALESCE(
                            MAX(version),
                            0
                        )
                    FROM documents
                    WHERE student_id = ?
                      AND document_type = ?
                    """,
                    (
                        student_id,
                        document_type,
                    ),
                ).fetchone()[0]
            )

            cursor = get_db().execute(
                """
                INSERT INTO documents (
                    student_id,
                    document_type,
                    original_name,
                    stored_name,
                    mime_type,
                    version,
                    source_document_id,
                    uploaded_by,
                    uploaded_at,
                    ocr_status,
                    page_count,
                    validation_status
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    'processing',
                    ?,
                    'checking'
                )
                """,
                (
                    student_id,
                    document_type,
                    original_name,
                    stored_name,
                    mime_type,
                    latest_version + 1,
                    source_document_id,
                    g.current_user["id"],
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    page_count,
                ),
            )

            document_id = (
                cursor.lastrowid
            )

            get_db().execute(
                """
                UPDATE students
                SET updated_at = ?
                WHERE id = ?
                """,
                (
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    student_id,
                ),
            )

            get_db().commit()

            audit(
                "document_uploaded_v6",
                student_id,
                (
                    f"{document_type}; "
                    f"document_id="
                    f"{document_id}; "
                    f"version="
                    f"{latest_version + 1}"
                ),
            )

            try:
                run_validation(
                    document_id
                )
            except Exception as e:
                flash(
                    f"Документ загружен, но проверка OCR не выполнена: {e}",
                    "warning",
                )

            return redirect(
                url_for(
                    "validation_result_v6",
                    document_id=document_id,
                )
            )

        body = """
        <h1>{{ document_config.name }}</h1>

        <div class="alert info">
            <strong>
                Автоматическая предварительная проверка
            </strong><br><br>

            После загрузки портал попытается определить,
            тот ли документ приложен, и сверит доступные
            данные с карточкой ученика.

            <br><br>

            🟢 соответствует —
            разрешено автозаполнение;<br>

            🟡 требуется ручная проверка —
            автозаполнение не выполняется;<br>

            🔴 обнаружено несовпадение —
            автозаполнение блокируется.
        </div>

        <form
            class="form-section"
            method="post"
            enctype="multipart/form-data"
        >
            <input
                type="hidden"
                name="csrf_token"
                value="{{ csrf_token }}"
            >

            <div>
                <label>
                    Файл или несколько страниц
                </label>

                <input
                    type="file"
                    name="files"
                    accept=".pdf,.jpg,.jpeg,.png"
                    multiple
                >

                <p class="muted">
                    PDF, JPG, JPEG, PNG.
                    Можно выбрать несколько страниц.
                </p>
            </div>

            <div
                class="space"
                style="
                    display:flex;
                    gap:12px;
                    flex-wrap:wrap;
                "
            >
                <button
                    class="btn btn-primary"
                    type="submit"
                >
                    Загрузить и проверить
                </button>

                <a
                    class="btn btn-secondary"
                    href="{{ url_for(
                        'student_detail',
                        student_id=student.id
                    ) }}"
                >
                    Назад
                </a>
            </div>
        </form>
        """

        return render_page(
            "Загрузка документа",
            body,
            student=student,
            document_config=(
                document_types[
                    document_type
                ]
            ),
            existing_documents=(
                existing_documents
            ),
            document_types=(
                document_types
            ),
        )

    # ========================================================
    # Экран результата (без изменений)
    # ========================================================

    def validation_result_v6(
        document_id: int,
    ):
        document = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

        if not document:
            abort(404)

        student = get_student_or_404(
            document["student_id"]
        )

        try:
            details = json.loads(
                document[
                    "validation_details"
                ]
                or "{}"
            )

        except json.JSONDecodeError:
            details = {}

        status = (
            document[
                "validation_status"
            ]
            or "warning"
        )

        presentation = {
            "passed": {
                "icon": "✓",
                "title":
                    "Документ прошел проверку",
                "color": "#147d3f",
                "background":
                    "#def6e7",
            },

            "warning": {
                "icon": "!",
                "title":
                    "Нужна дополнительная проверка",
                "color": "#9a6a00",
                "background":
                    "#fff7c2",
            },

            "mismatch": {
                "icon": "×",
                "title":
                    "Обнаружено несовпадение",
                "color": "#c62828",
                "background":
                    "#ffe4e4",
            },
        }.get(
            status,
            {
                "icon": "?",
                "title":
                    "Результат проверки",
                "color": "#666",
                "background":
                    "#eee",
            },
        )

        detected_code = (
            document[
                "detected_document_type"
            ]
            or "unknown"
        )

        detected_label = (
            DETECTED_LABELS.get(
                detected_code,
                detected_code,
            )
        )

        confidence = (
            document[
                "validation_confidence"
            ]
            or 0
        )

        body = """
        <h1>
            Результат предварительной проверки
        </h1>

        <div
            class="card"
            style="
                border-left:
                    8px solid {{ presentation.color }};
                background:
                    {{ presentation.background }};
            "
        >
            <div
                style="
                    display:flex;
                    gap:16px;
                    align-items:center;
                "
            >
                <div
                    style="
                        width:54px;
                        height:54px;
                        border-radius:50%;
                        display:grid;
                        place-items:center;
                        font-size:34px;
                        font-weight:800;
                        color:white;
                        background:
                            {{ presentation.color }};
                    "
                >
                    {{ presentation.icon }}
                </div>

                <div>
                    <h2 style="margin:0">
                        {{ presentation.title }}
                    </h2>

                    <p style="margin-bottom:0">
                        {{
                            document.validation_message
                            or ''
                        }}
                    </p>
                </div>
            </div>
        </div>

        <div class="card space">
            <table>
                <tr>
                    <th>Ожидался документ</th>
                    <td>
                        {{
                            document_types[
                                document.document_type
                            ].name
                        }}
                    </td>
                </tr>

                <tr>
                    <th>Определено системой</th>
                    <td>{{ detected_label }}</td>
                </tr>

                <tr>
                    <th>Уверенность классификации</th>
                    <td>
                        {{
                            "%.0f"|format(
                                confidence * 100
                            )
                        }}%
                    </td>
                </tr>

                <tr>
                    <th>Файл</th>
                    <td>
                        {{ document.original_name }}
                    </td>
                </tr>

                <tr>
                    <th>Версия</th>
                    <td>
                        {{ document.version }}
                    </td>
                </tr>
            </table>
        </div>

        {% if details.checks %}
            <h2>Что проверено</h2>

            <div class="card">
                {% for check in details.checks %}
                    <div
                        style="
                            padding:12px 0;
                            border-bottom:
                                1px solid #eee;
                        "
                    >
                        {% if check.level == 'green' %}
                            <strong
                                style="color:#147d3f"
                            >
                                ✓
                            </strong>

                        {% elif check.level == 'red' %}
                            <strong
                                style="color:#c62828"
                            >
                                ×
                            </strong>

                        {% else %}
                            <strong
                                style="color:#9a6a00"
                            >
                                !
                            </strong>
                        {% endif %}

                        {{ check.text }}
                    </div>
                {% endfor %}
            </div>
        {% endif %}

        {% if document.validation_status == 'passed' %}
            <div class="alert success space">
                <strong>
                    Проверка пройдена.
                </strong>
                <br>
                После нажатия «Сохранить»
                система может автоматически
                заполнить только пустые поля
                карточки.
            </div>

        {% elif document.validation_status == 'mismatch' %}
            <div class="alert error space">
                <strong>
                    Автозаполнение заблокировано.
                </strong>
                <br>
                Документ можно сохранить.
                После сохранения он будет отмечен
                для ручной проверки и до решения
                проверяющего не будет закрывать
                обязательную позицию.
            </div>

        {% else %}
            <div class="alert info space">
                <strong>
                    Автозаполнение не выполняется.
                </strong>
                <br>
                Документ можно сохранить.
                После сохранения он будет отмечен
                как требующий ручной проверки.
            </div>
        {% endif %}

        <div
            class="space"
            style="
                display:flex;
                gap:10px;
                flex-wrap:wrap;
            "
        >
            {% if document.save_confirmed %}

                <span
                    class="btn btn-secondary"
                    style="
                        cursor:default;
                        opacity:.8;
                    "
                >
                    ✓ Сохранено
                </span>

            {% elif document.validation_status in
                ['passed', 'warning', 'mismatch'] %}

                <form
                    class="inline"
                    method="post"
                    action="{{ url_for(
                        'save_validated_document_v7',
                        document_id=document.id
                    ) }}"
                >
                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{{ csrf_token }}"
                    >

                    <button
                        class="btn btn-primary"
                        type="submit"
                    >
                        Сохранить
                    </button>
                </form>

            {% else %}

                <button
                    class="btn btn-primary"
                    type="button"
                    disabled
                    style="
                        opacity:.45;
                        cursor:not-allowed;
                    "
                >
                    Сохранить
                </button>

            {% endif %}

            <a
                class="btn btn-secondary"
                target="_blank"
                href="{{ url_for(
                    'download_document',
                    document_id=document.id
                ) }}"
            >
                Открыть документ
            </a>

            <a
                class="btn btn-primary"
                href="{{ url_for(
                    'upload_document',
                    student_id=student.id,
                    document_type=
                        document.document_type
                ) }}"
            >
                Загрузить другой файл
            </a>

            <a
                class="btn btn-secondary"
                href="{{ url_for(
                    'student_detail',
                    student_id=student.id
                ) }}"
            >
                Вернуться к карточке
            </a>

            <form
                class="inline"
                method="post"
                action="{{ url_for(
                    'validation_recheck_v6',
                    document_id=document.id
                ) }}"
            >
                <input
                    type="hidden"
                    name="csrf_token"
                    value="{{ csrf_token }}"
                >

                <button
                    class="btn btn-secondary"
                    type="submit"
                >
                    Повторить проверку
                </button>
            </form>
        </div>
        """

        return render_page(
            "Результат проверки",
            body,
            student=student,
            document=document,
            document_types=document_types,
            details=details,
            presentation=presentation,
            detected_label=detected_label,
            confidence=confidence,
        )

    def validation_recheck_v6(
        document_id: int,
    ):
        document = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

        if not document:
            abort(404)

        student = get_student_or_404(
            document["student_id"]
        )

        if (
            g.current_user["role"] == "branch"
            and student["branch_id"]
            != g.current_user["branch_id"]
        ):
            abort(403)

        run_validation(
            document_id
        )

        return redirect(
            url_for(
                "validation_result_v6",
                document_id=document_id,
            )
        )

    # ========================================================
    # Подключаем v6
    # ========================================================

    app.view_functions[
        "upload_document"
    ] = roles_required(
        "branch"
    )(
        upload_document_v6
    )

    if (
        "validation_result_v6"
        not in app.view_functions
    ):
        app.add_url_rule(
            "/documents/"
            "<int:document_id>/validation",
            endpoint=
                "validation_result_v6",
            view_func=login_required(
                validation_result_v6
            ),
            methods=["GET"],
        )

    if (
        "validation_recheck_v6"
        not in app.view_functions
    ):
        app.add_url_rule(
            "/documents/"
            "<int:document_id>/validation/recheck",
            endpoint=
                "validation_recheck_v6",
            view_func=login_required(
                validation_recheck_v6
            ),
            methods=["POST"],
        )