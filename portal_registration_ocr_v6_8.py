# REGISTRATION FIO OCR V6.8.1
# CLOUD COMPATIBLE

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile

from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import fitz

from flask import (
    flash,
    redirect,
    request,
    url_for,
)

from PIL import (
    Image,
    ImageOps,
)

# Импортируем облачный модуль OCR
from portal_ocr_cloud import get_paddle_ocr_safe, ocr_configured


DOCUMENT_TYPE = "child_registration"


def apply_registration_ocr_v6_8(
    app,
    namespace: dict[str, Any],
) -> None:

    if app.extensions.get(
        "registration_ocr_v6_8"
    ):
        return

    app.extensions[
        "registration_ocr_v6_8"
    ] = True

    database_path: Path = namespace[
        "DATABASE_PATH"
    ]

    upload_dir: Path = namespace[
        "UPLOAD_DIR"
    ]

    _engine = None
    _engine_error = None

    # ========================================================
    # Добавляем документ в расширенный
    # блок проверки отдела аттестации.
    # ========================================================

    try:
        import portal_attestation_tools_v9 \
            as attestation_tools

        attestation_tools.DOCUMENT_FIELDS[
            DOCUMENT_TYPE
        ] = [
            (
                "last_name",
                "Фамилия ребенка",
                "text",
            ),
            (
                "first_name",
                "Имя ребенка",
                "text",
            ),
            (
                "middle_name",
                "Отчество ребенка",
                "text",
            ),
            (
                "birth_date",
                "Дата рождения",
                "date",
            ),
        ]

    except Exception:
        # Модуль рабочего места
        # аттестации может быть
        # не установлен.
        pass

    # ========================================================
    # База
    # ========================================================

    def connect():

        connection = sqlite3.connect(
            database_path
        )

        connection.row_factory = (
            sqlite3.Row
        )

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection


    def document_columns(
        connection,
    ):

        return {
            row[1]
            for row
            in connection.execute(
                "PRAGMA table_info(documents)"
            ).fetchall()
        }


    def get_document_and_student(
        document_id: int,
    ):

        connection = connect()

        try:

            document = connection.execute(
                """
                SELECT *
                FROM documents
                WHERE id = ?
                """,
                (
                    document_id,
                ),
            ).fetchone()

            if not document:

                raise RuntimeError(
                    "Документ не найден."
                )

            student = connection.execute(
                """
                SELECT *
                FROM students
                WHERE id = ?
                """,
                (
                    document[
                        "student_id"
                    ],
                ),
            ).fetchone()

            if not student:

                raise RuntimeError(
                    "Карточка ученика "
                    "не найдена."
                )

            return (
                dict(document),
                dict(student),
            )

        finally:

            connection.close()

    # ========================================================
    # PaddleOCR
    # ========================================================

    def get_engine():
        """Получение экземпляра PaddleOCR с использованием облачного модуля. """
        nonlocal _engine
        nonlocal _engine_error

        if _engine is not None:
            return _engine

        if _engine_error is not None:
            raise RuntimeError(_engine_error)

        # Используем безопасную инициализацию из облачного модуля
        engine = get_paddle_ocr_safe()
        
        if engine is None:
            _engine_error = "PaddleOCR не доступен в облачной среде"
            raise RuntimeError(_engine_error)

        _engine = engine
        return _engine


    def to_list(
        value,
    ):

        if value is None:
            return []

        if hasattr(
            value,
            "tolist",
        ):
            return value.tolist()

        if isinstance(
            value,
            (
                list,
                tuple,
            ),
        ):
            return list(
                value
            )

        return []


    def result_to_dict(
        item,
    ):

        data = item

        if hasattr(
            data,
            "json",
        ):

            data = data.json

            if callable(data):
                data = data()

        if isinstance(
            data,
            str,
        ):

            data = json.loads(
                data
            )

        if (
            isinstance(
                data,
                dict,
            )
            and isinstance(
                data.get(
                    "res"
                ),
                dict,
            )
        ):

            data = data[
                "res"
            ]

        if not isinstance(
            data,
            dict,
        ):
            return {}

        return data


    def run_ocr(
        image: Image.Image,
    ):

        image = (
            ImageOps.exif_transpose(
                image
            )
            .convert(
                "RGB"
            )
        )

        maximum = max(
            image.size
        )

        if maximum > 2600:

            scale = (
                2600.0
                / maximum
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

        temp_path = None

        try:

            with tempfile.NamedTemporaryFile(
                suffix=".jpg",
                delete=False,
            ) as file:

                temp_path = (
                    file.name
                )

            image.save(
                temp_path,
                format="JPEG",
                quality=94,
            )

            prediction = (
                get_engine().predict(
                    temp_path,

                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,

                    text_det_limit_side_len=2400,
                    text_det_limit_type="max",
                )
            )

            blocks = []

            for item in prediction:

                data = (
                    result_to_dict(
                        item
                    )
                )

                texts = to_list(
                    data.get(
                        "rec_texts"
                    )
                )

                scores = to_list(
                    data.get(
                        "rec_scores"
                    )
                )

                boxes = to_list(
                    data.get(
                        "rec_boxes"
                    )
                )

                for index, text in (
                    enumerate(
                        texts
                    )
                ):

                    text = str(
                        text or ""
                    ).strip()

                    if not text:
                        continue

                    try:

                        score = (
                            float(
                                scores[
                                    index
                                ]
                            )

                            if index
                            < len(scores)

                            else 0.0
                        )

                    except (
                        TypeError,
                        ValueError,
                    ):

                        score = 0.0

                    box = (
                        boxes[index]

                        if index
                        < len(boxes)

                        else None
                    )

                    if (
                        isinstance(
                            box,
                            (
                                list,
                                tuple,
                            ),
                        )
                        and len(box)
                        >= 4
                    ):

                        try:

                            (
                                x1,
                                y1,
                                x2,
                                y2,
                            ) = map(
                                float,
                                box[:4],
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            (
                                x1,
                                y1,
                                x2,
                                y2,
                            ) = (
                                0,
                                0,
                                0,
                                0,
                            )

                    else:

                        (
                            x1,
                            y1,
                            x2,
                            y2,
                        ) = (
                            0,
                            0,
                            0,
                            0,
                        )

                    blocks.append(
                        {
                            "text":
                                text,

                            "score":
                                round(
                                    score,
                                    4,
                                ),

                            "box": [
                                round(
                                    x1,
                                    1,
                                ),
                                round(
                                    y1,
                                    1,
                                ),
                                round(
                                    x2,
                                    1,
                                ),
                                round(
                                    y2,
                                    1,
                                ),
                            ],
                        }
                    )

            return blocks

        finally:

            if (
                temp_path
                and os.path.exists(
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
    # PDF / изображение
    # ========================================================

    def load_pages(
        path: Path,
    ):

        if (
            path.suffix.lower()
            == ".pdf"
        ):

            document = fitz.open(
                path
            )

            try:

                pages = []

                for index in range(
                    min(
                        len(document),
                        3,
                    )
                ):

                    page = (
                        document.load_page(
                            index
                        )
                    )

                    pixmap = (
                        page.get_pixmap(
                            matrix=fitz.Matrix(
                                2.2,
                                2.2,
                            ),
                            alpha=False,
                        )
                    )

                    image = (
                        Image.frombytes(
                            "RGB",
                            (
                                pixmap.width,
                                pixmap.height,
                            ),
                            pixmap.samples,
                        )
                    )

                    pages.append(
                        image
                    )

                return pages

            finally:

                document.close()

        image = Image.open(
            path
        )

        image.load()

        return [
            ImageOps.exif_transpose(
                image
            ).convert(
                "RGB"
            )
        ]


    def rotated(
        image,
        angle,
    ):

        if angle == 0:

            return image.copy()

        return image.rotate(
            angle,
            expand=True,
            fillcolor="white",
        )

    # ========================================================
    # Нормализация
    # ========================================================

    def normalize(
        value,
    ):

        value = (
            str(
                value or ""
            )
            .lower()
            .replace(
                "ё",
                "е",
            )
        )

        value = re.sub(
            r"\s+",
            " ",
            value,
        )

        return value.strip()


    def normalize_name(
        value,
    ):

        return re.sub(
            r"[^а-яa-z-]",
            "",
            normalize(
                value
            ),
        )


    def smart_title(
        value,
    ):

        value = str(
            value or ""
        ).strip()

        if not value:
            return ""

        return "-".join(
            part[:1].upper()
            + part[1:].lower()

            for part in value.split(
                "-"
            )

            if part
        )


    def clean_name_word(
        value,
    ):

        return smart_title(
            re.sub(
                r"[^А-Яа-яЁё-]",
                "",
                str(
                    value or ""
                ),
            )
        )


    def reading_order(
        blocks,
    ):

        return sorted(
            blocks,
            key=lambda block: (
                round(
                    block[
                        "box"
                    ][1]
                    / 15
                ),
                block[
                    "box"
                ][0],
            ),
        )


    def joined_text(
        blocks,
    ):

        return "\n".join(
            block["text"]

            for block in (
                reading_order(
                    blocks
                )
            )
        )

    # ========================================================
    # Определение документа
    # ========================================================

    def registration_score(
        blocks,
    ):

        text = normalize(
            " ".join(
                block["text"]
                for block in blocks
            )
        )

        score = 0

        if (
            "свидетельство"
            in text
        ):
            score += 3

        if (
            "регистрац"
            in text
        ):
            score += 4

        if (
            "месту жительства"
            in text
        ):
            score += 5

        if (
            "месту пребывания"
            in text
        ):
            score += 5

        if (
            "зарегистрирован"
            in text
        ):
            score += 4

        if (
            "по адресу"
            in text
        ):
            score += 3

        if (
            "выдано"
            in text
        ):
            score += 1

        return score


    def best_orientation(
        image,
    ):

        attempts = []

        # В приложенном примере
        # документ повернут,
        # поэтому проверяем
        # четыре ориентации.
        for angle in (
            0,
            90,
            270,
            180,
        ):

            variant = rotated(
                image,
                angle,
            )

            blocks = run_ocr(
                variant
            )

            score = (
                registration_score(
                    blocks
                )
            )

            attempts.append(
                {
                    "angle":
                        angle,

                    "image":
                        variant,

                    "blocks":
                        blocks,

                    "score":
                        score,
                }
            )

            if score >= 15:
                break

        attempts.sort(
            key=lambda item:
                item[
                    "score"
                ],
            reverse=True,
        )

        return attempts[0]

    # ========================================================
    # ФИО после слова «Выдано»
    # ========================================================

    def extract_fio_from_text(
        text,
    ):

        # Основной вариант свидетельств:
        #
        # Выдано
        # ГАЛКИН АРТУР МАКСИМОВИЧ,
        # 04.01.2017 ...
        pattern = re.compile(
            r"выдан[оа]?\s*"
            r"([А-ЯЁ][А-ЯЁа-яё-]{1,})"
            r"\s+"
            r"([А-ЯЁ][А-ЯЁа-яё-]{1,})"
            r"\s+"
            r"([А-ЯЁ][А-ЯЁа-яё-]{1,})",
            flags=re.IGNORECASE,
        )

        match = pattern.search(
            text
        )

        if not match:

            return {
                "last_name": "",
                "first_name": "",
                "middle_name": "",
            }

        return {
            "last_name":
                clean_name_word(
                    match.group(1)
                ),

            "first_name":
                clean_name_word(
                    match.group(2)
                ),

            "middle_name":
                clean_name_word(
                    match.group(3)
                ),
        }

    # ========================================================
    # Fallback: ищем ожидаемое ФИО
    # среди отдельных OCR-блоков.
    # ========================================================


    # ========================================================
    # REGISTRATION FIO OCR V6.8.1
    #
    # PaddleOCR может вернуть:
    #
    # "ГАЛКИН АРТУР МАКСИМОВИЧ"
    #
    # одним блоком. Поэтому сравниваем
    # не весь блок целиком, а каждое
    # русское слово внутри него.
    # ========================================================

    def name_tokens_from_blocks(
        blocks,
    ):

        result = []

        ignored = {
            "выдано",
            "выдан",
            "свидетельство",
            "регистрации",
            "регистрация",
            "месту",
            "жительства",
            "пребывания",
            "россия",
            "российская",
            "федерация",
            "область",
            "район",
            "город",
            "адресу",
            "адрес",
            "дата",
            "рождения",
        }

        for block in blocks:

            text = str(
                block.get(
                    "text",
                    "",
                )
            )

            words = re.findall(
                r"[А-ЯЁа-яё-]{2,}",
                text,
            )

            for word in words:

                cleaned = (
                    clean_name_word(
                        word
                    )
                )

                if not cleaned:
                    continue

                normalized = (
                    normalize_name(
                        cleaned
                    )
                )

                if (
                    not normalized
                    or normalized
                    in ignored
                ):
                    continue

                result.append(
                    {
                        "value":
                            cleaned,

                        "normalized":
                            normalized,

                        "score":
                            float(
                                block.get(
                                    "score",
                                    0.0,
                                )
                                or 0.0
                            ),

                        "source":
                            text,
                    }
                )

        return result


    def find_expected_name_part(
        blocks,
        expected,
    ):

        if not expected:

            return (
                "",
                0.0,
            )

        expected_normalized = (
            normalize_name(
                expected
            )
        )

        if not expected_normalized:

            return (
                "",
                0.0,
            )

        best_similarity = 0.0
        best_value = ""

        for candidate in (
            name_tokens_from_blocks(
                blocks
            )
        ):

            candidate_normalized = (
                candidate[
                    "normalized"
                ]
            )

            # Точное совпадение.
            if (
                candidate_normalized
                == expected_normalized
            ):

                return (
                    candidate[
                        "value"
                    ],
                    1.0,
                )

            similarity = (
                SequenceMatcher(
                    None,
                    candidate_normalized,
                    expected_normalized,
                ).ratio()
            )

            # Защита от случайного
            # совпадения коротких слов.
            length_difference = abs(
                len(
                    candidate_normalized
                )
                -
                len(
                    expected_normalized
                )
            )

            if (
                length_difference
                > 3
            ):
                similarity *= 0.75

            if (
                similarity
                > best_similarity
            ):

                best_similarity = (
                    similarity
                )

                best_value = (
                    candidate[
                        "value"
                    ]
                )

        return (
            best_value,
            best_similarity,
        )


    def focused_name_ocr(
        image,
        base_blocks,
    ):

        """
        Второй OCR только области,
        в которой расположена строка
        «Выдано ФИО...».

        Сначала пытаемся определить
        положение слова «Выдано».
        Если OCR его не нашел —
        проверяем верхнюю половину
        свидетельства.
        """

        width, height = (
            image.size
        )

        anchor_blocks = [
            block
            for block in base_blocks
            if (
                "выдан"
                in normalize(
                    block.get(
                        "text",
                        "",
                    )
                )
            )
        ]

        crops = []

        if anchor_blocks:

            for anchor in (
                anchor_blocks[:2]
            ):

                (
                    x1,
                    y1,
                    x2,
                    y2,
                ) = anchor[
                    "box"
                ]

                center_y = (
                    y1 + y2
                ) / 2

                top = max(
                    0,
                    int(
                        center_y
                        - height
                        * 0.07
                    ),
                )

                bottom = min(
                    height,
                    int(
                        center_y
                        + height
                        * 0.16
                    ),
                )

                crops.append(
                    image.crop(
                        (
                            0,
                            top,
                            width,
                            bottom,
                        )
                    )
                )

        # Запасной вариант.
        if not crops:

            crops.append(
                image.crop(
                    (
                        0,
                        int(
                            height
                            * 0.05
                        ),
                        width,
                        int(
                            height
                            * 0.46
                        ),
                    )
                )
            )

        result_blocks = []

        for crop in crops:

            # -----------------------------
            # Вариант 1:
            # обычное изображение
            # -----------------------------

            maximum = max(
                crop.size
            )

            if maximum < 1900:

                scale = min(
                    2.2,
                    1900.0
                    / maximum,
                )

                enlarged = crop.resize(
                    (
                        max(
                            1,
                            int(
                                crop.width
                                * scale
                            ),
                        ),
                        max(
                            1,
                            int(
                                crop.height
                                * scale
                            ),
                        ),
                    ),
                    Image.Resampling.LANCZOS,
                )

            else:

                enlarged = crop

            try:

                result_blocks.extend(
                    run_ocr(
                        enlarged
                    )
                )

            except Exception:

                pass

            # -----------------------------
            # Вариант 2:
            # повышаем контраст
            # слабой серой печати
            # -----------------------------

            try:

                gray = (
                    ImageOps.grayscale(
                        enlarged
                    )
                )

                gray = (
                    ImageOps.autocontrast(
                        gray,
                        cutoff=1,
                    )
                )

                contrast_image = (
                    gray.convert(
                        "RGB"
                    )
                )

                result_blocks.extend(
                    run_ocr(
                        contrast_image
                    )
                )

            except Exception:

                pass

        return result_blocks


    def extract_registration_fio(
        text,
        blocks,
        image,
        expected,
    ):

        """
        Последовательность:

        1. стандартный поиск после
           слова «Выдано»;

        2. поиск каждого элемента ФИО
           внутри ВСЕХ слов OCR;

        3. если чего-то не хватает —
           повторный OCR области ФИО.
        """

        fio = (
            extract_fio_from_text(
                text
            )
        )

        # --------------------------------
        # Основные OCR-блоки.
        # --------------------------------

        for field in (
            "last_name",
            "first_name",
            "middle_name",
        ):

            if fio.get(
                field
            ):
                continue

            (
                candidate,
                similarity,
            ) = find_expected_name_part(
                blocks,
                expected.get(
                    field,
                    "",
                ),
            )

            if (
                candidate
                and similarity
                >= 0.68
            ):

                fio[
                    field
                ] = candidate

        # --------------------------------
        # Прицельный повторный OCR.
        # --------------------------------

        missing = [
            field
            for field in (
                "last_name",
                "first_name",
                "middle_name",
            )
            if (
                expected.get(
                    field
                )
                and not fio.get(
                    field
                )
            )
        ]

        if missing:

            focused_blocks = (
                focused_name_ocr(
                    image,
                    blocks,
                )
            )

            for field in missing:

                (
                    candidate,
                    similarity,
                ) = (
                    find_expected_name_part(
                        focused_blocks,
                        expected.get(
                            field,
                            "",
                        ),
                    )
                )

                if (
                    candidate
                    and similarity
                    >= 0.68
                ):

                    fio[
                        field
                    ] = candidate

            # Сохраняем дополнительный OCR
            # для диагностики.
            return (
                fio,
                focused_blocks,
            )

        return (
            fio,
            [],
        )


    # ========================================================
    # Даты
    # ========================================================

    def parse_date(
        value,
    ):

        match = re.search(
            r"(?<!\d)"
            r"([0-3]?\d)"
            r"\s*[./-]\s*"
            r"([01]?\d)"
            r"\s*[./-]\s*"
            r"((?:19|20)\d{2})"
            r"(?!\d)",
            str(
                value or ""
            ),
        )

        if not match:

            return ""

        (
            day,
            month,
            year,
        ) = map(
            int,
            match.groups(),
        )

        try:

            parsed = datetime(
                year,
                month,
                day,
            )

        except ValueError:

            return ""

        return parsed.strftime(
            "%Y-%m-%d"
        )


    def all_dates(
        text,
    ):

        result = []

        pattern = re.compile(
            r"(?<!\d)"
            r"[0-3]?\d"
            r"\s*[./-]\s*"
            r"[01]?\d"
            r"\s*[./-]\s*"
            r"(?:19|20)\d{2}"
            r"(?!\d)"
        )

        for match in pattern.finditer(
            text
        ):

            parsed = parse_date(
                match.group(0)
            )

            if (
                parsed
                and parsed
                not in result
            ):

                result.append(
                    parsed
                )

        return result


    def display_date(
        value,
    ):

        if not value:
            return ""

        try:

            return (
                datetime.strptime(
                    value,
                    "%Y-%m-%d",
                )
                .strftime(
                    "%d.%m.%Y"
                )
            )

        except ValueError:

            return value

    # ========================================================
    # Номер свидетельства
    # ========================================================

    def extract_certificate_number(
        text,
    ):

        patterns = [
            re.compile(
                r"свидетельство"
                r"\s*(?:№|N|No|#)?"
                r"\s*(\d{2,10})",
                flags=re.IGNORECASE,
            ),

            re.compile(
                r"№\s*(\d{2,10})",
                flags=re.IGNORECASE,
            ),
        ]

        for pattern in patterns:

            match = pattern.search(
                text
            )

            if match:

                return (
                    match.group(1)
                )

        return ""

    # ========================================================
    # Адрес
    # ========================================================

    def extract_address(
        text,
    ):

        # Делаем одну строку,
        # но сохраняем исходные слова.
        flat = re.sub(
            r"\s+",
            " ",
            text,
        )

        patterns = [
            re.compile(
                r"(?:по\s+месту\s+"
                r"жительства\s+)?"
                r"по\s+адресу\s*[:\-]?\s*"
                r"(.{10,300}?)"
                r"(?="
                r"свидетельство\s+выдан"
                r"|место\s+выдачи"
                r"|дата\s+выдачи"
                r"|$"
                r")",
                flags=(
                    re.IGNORECASE
                    | re.DOTALL
                ),
            ),
        ]

        for pattern in patterns:

            match = pattern.search(
                flat
            )

            if match:

                address = (
                    match.group(1)
                    .strip(
                        " ,.;:-"
                    )
                )

                return address[
                    :300
                ]

        return ""

    # ========================================================
    # Сравнение ФИО
    # ========================================================

    def name_similarity(
        left,
        right,
    ):

        if (
            not left
            or not right
        ):
            return 0.0

        return (
            SequenceMatcher(
                None,
                normalize_name(
                    left
                ),
                normalize_name(
                    right
                ),
            ).ratio()
        )


    def check_name_part(
        label,
        recognized,
        expected,
        checks,
    ):

        if not expected:

            if recognized:

                checks.append(
                    {
                        "level":
                            "green",

                        "text":
                            f"{label} "
                            f"распознано: "
                            f"{recognized}.",
                    }
                )

            return (
                "ok",
                bool(
                    recognized
                ),
            )

        if not recognized:

            checks.append(
                {
                    "level":
                        "yellow",

                    "text":
                        f"{label} "
                        "не удалось "
                        "распознать.",
                }
            )

            return (
                "warning",
                False,
            )

        similarity = (
            name_similarity(
                recognized,
                expected,
            )
        )

        if similarity >= 0.94:

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        f"{label} "
                        f"совпадает: "
                        f"{recognized}.",
                }
            )

            return (
                "ok",
                True,
            )

        if similarity >= 0.76:

            checks.append(
                {
                    "level":
                        "yellow",

                    "text":
                        f"{label} "
                        "распознано как "
                        f"«{recognized}». "
                        "Требуется "
                        "ручная проверка.",
                }
            )

            return (
                "warning",
                False,
            )

        checks.append(
            {
                "level":
                    "red",

                "text":
                    f"{label} "
                    "не совпадает. "
                    f"Документ: "
                    f"«{recognized}», "
                    f"карточка: "
                    f"«{expected}».",
            }
        )

        return (
            "mismatch",
            False,
        )

    # ========================================================
    # Основное распознавание
    # ========================================================

    def recognize_registration(
        path: Path,
        student,
    ):

        pages = load_pages(
            path
        )

        if not pages:

            raise RuntimeError(
                "В файле нет страниц."
            )

        page_results = []

        for index, page in (
            enumerate(
                pages
            )
        ):

            result = (
                best_orientation(
                    page
                )
            )

            result[
                "page_index"
            ] = index

            page_results.append(
                result
            )

        page_results.sort(
            key=lambda item:
                item[
                    "score"
                ],
            reverse=True,
        )

        best = (
            page_results[0]
        )

        blocks = best[
            "blocks"
        ]

        text = joined_text(
            blocks
        )

        type_score = best[
            "score"
        ]

        expected = {
            "last_name":
                student.get(
                    "last_name"
                )
                or "",

            "first_name":
                student.get(
                    "first_name"
                )
                or "",

            "middle_name":
                student.get(
                    "middle_name"
                )
                or "",

            "birth_date":
                student.get(
                    "birth_date"
                )
                or "",
        }

        # --------------------------------
        # Улучшенное распознавание ФИО
        # v6.8.1.
        #
        # Учитывает ситуацию, когда
        # PaddleOCR возвращает всё ФИО
        # одной строкой.
        # --------------------------------

        (
            fio,
            focused_name_blocks,
        ) = extract_registration_fio(
            text,
            blocks,
            best["image"],
            expected,
        )

        dates = all_dates(
            text
        )

        recognized_birth_date = ""

        # Наиболее надежная проверка:
        # дата из карточки реально
        # присутствует в документе.
        if (
            expected[
                "birth_date"
            ]
            in dates
        ):

            recognized_birth_date = (
                expected[
                    "birth_date"
                ]
            )

        certificate_number = (
            extract_certificate_number(
                text
            )
        )

        address = extract_address(
            text
        )

        # ====================================================
        # Что проверено
        # ====================================================

        checks = []

        if type_score >= 10:

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        "Тип документа "
                        "соответствует "
                        "ожидаемому: "
                        "подтверждение "
                        "регистрации ребенка.",
                }
            )

        elif type_score >= 6:

            checks.append(
                {
                    "level":
                        "yellow",

                    "text":
                        "Документ похож "
                        "на подтверждение "
                        "регистрации ребенка, "
                        "но требуется "
                        "ручная проверка.",
                }
            )

        else:

            checks.append(
                {
                    "level":
                        "red",

                    "text":
                        "Не удалось "
                        "подтвердить, что "
                        "загружен документ "
                        "о регистрации.",
                }
            )

        states = []

        matched_names = 0

        for (
            field,
            label,
        ) in (
            (
                "last_name",
                "Фамилия ребенка",
            ),
            (
                "first_name",
                "Имя ребенка",
            ),
            (
                "middle_name",
                "Отчество ребенка",
            ),
        ):

            (
                state,
                matched,
            ) = check_name_part(
                label,
                fio[
                    field
                ],
                expected[
                    field
                ],
                checks,
            )

            states.append(
                state
            )

            matched_names += int(
                matched
            )

        # --------------------------------
        # Дата рождения
        # --------------------------------

        if (
            expected[
                "birth_date"
            ]
            and recognized_birth_date
        ):

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        "Дата рождения "
                        "совпадает: "
                        + display_date(
                            recognized_birth_date
                        )
                        + ".",
                }
            )

            states.append(
                "ok"
            )

        elif expected[
            "birth_date"
        ]:

            checks.append(
                {
                    "level":
                        "yellow",

                    "text":
                        "Дата рождения "
                        "ребенка не удалось "
                        "надежно подтвердить "
                        "по OCR.",
                }
            )

            states.append(
                "warning"
            )

        # --------------------------------
        # Номер свидетельства
        # --------------------------------

        if certificate_number:

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        "Номер свидетельства "
                        "распознан: "
                        f"{certificate_number}.",
                }
            )

        # --------------------------------
        # Адрес
        # --------------------------------

        if address:

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        "Сведения об адресе "
                        "регистрации "
                        "распознаны.",
                }
            )

        # ====================================================
        # Итоговый статус
        # ====================================================

        if (
            type_score < 6
        ):

            status = (
                "mismatch"
            )

        elif (
            "mismatch"
            in states
        ):

            status = (
                "mismatch"
            )

        elif (
            type_score >= 10
            and matched_names
            >= 2
            and (
                not expected[
                    "birth_date"
                ]
                or recognized_birth_date
            )
        ):

            status = (
                "passed"
            )

        else:

            status = (
                "warning"
            )

        confidence = (
            0.98
            if (
                type_score >= 14
                and status
                == "passed"
            )

            else 0.92
            if (
                type_score >= 10
                and status
                == "passed"
            )

            else 0.72
            if type_score >= 6

            else 0.35
        )

        # Только реальные значения,
        # полученные из OCR.
        candidates = {}

        if fio[
            "last_name"
        ]:

            candidates[
                "last_name"
            ] = fio[
                "last_name"
            ]

        if fio[
            "first_name"
        ]:

            candidates[
                "first_name"
            ] = fio[
                "first_name"
            ]

        if fio[
            "middle_name"
        ]:

            candidates[
                "middle_name"
            ] = fio[
                "middle_name"
            ]

        if recognized_birth_date:

            candidates[
                "birth_date"
            ] = (
                recognized_birth_date
            )

        registration = {
            "document_kind":
                (
                    "Подтверждение "
                    "регистрации ребенка"
                ),

            "last_name":
                fio[
                    "last_name"
                ],

            "first_name":
                fio[
                    "first_name"
                ],

            "middle_name":
                fio[
                    "middle_name"
                ],

            "birth_date":
                recognized_birth_date,

            "certificate_number":
                certificate_number,

            "address":
                address,

            "orientation":
                best[
                    "angle"
                ],

            "page_index":
                best[
                    "page_index"
                ],

            "classification_score":
                type_score,
        }

        return {
            "ocr_text":
                text,

            "ocr_json": {
                "engine":
                    (
                        "Registration "
                        "Smart OCR v6.8"
                    ),

                "registration":
                    registration,

                # Сохраняем координаты,
                # чтобы в дальнейшем
                # можно было улучшать
                # алгоритм без изменения
                # структуры БД.
                "blocks":
                    blocks,

                "focused_name_blocks":
                    focused_name_blocks,
            },

            "validation_status":
                status,

            "detected_document_type":
                DOCUMENT_TYPE,

            "validation_confidence":
                confidence,

            "validation_message":
                (
                    "Документ о регистрации "
                    "ребенка распознан "
                    "и проверен."

                    if status
                    == "passed"

                    else
                    "Документ о регистрации "
                    "распознан, но требуется "
                    "проверка результата."
                ),

            "validation_details": {
                "expected":
                    DOCUMENT_TYPE,

                "detected":
                    DOCUMENT_TYPE,

                "confidence":
                    confidence,

                "checks":
                    checks,

                "candidates":
                    candidates,

                "registration":
                    registration,
            },
        }

    # ========================================================
    # Сохранение результата
    # ========================================================

    def save_result(
        document_id,
        result,
    ):

        connection = connect()

        try:

            columns = (
                document_columns(
                    connection
                )
            )

            now = (
                datetime.now()
                .isoformat(
                    timespec="seconds"
                )
            )

            values = {
                "ocr_status":
                    "done",

                "ocr_text":
                    result[
                        "ocr_text"
                    ],

                "ocr_json":
                    json.dumps(
                        result[
                            "ocr_json"
                        ],
                        ensure_ascii=False,
                    ),

                "validation_status":
                    result[
                        "validation_status"
                    ],

                "detected_document_type":
                    result[
                        "detected_document_type"
                    ],

                "validation_confidence":
                    result[
                        "validation_confidence"
                    ],

                "validation_message":
                    result[
                        "validation_message"
                    ],

                "validation_details":
                    json.dumps(
                        result[
                            "validation_details"
                        ],
                        ensure_ascii=False,
                    ),

                "validated_at":
                    now,

                # Этот документ проверяет
                # данные карточки,
                # но не должен автоматически
                # переписывать их.
                "autofill_blocked":
                    1,

                "autofill_applied":
                    0,

                "manual_review_required":
                    (
                        0
                        if result[
                            "validation_status"
                        ]
                        == "passed"

                        else 1
                    ),
            }

            fields = [
                field
                for field
                in values

                if field
                in columns
            ]

            assignments = ", ".join(
                f"{field} = ?"
                for field
                in fields
            )

            connection.execute(
                f"""
                UPDATE documents
                SET {assignments}
                WHERE id = ?
                """,
                [
                    *[
                        values[
                            field
                        ]
                        for field
                        in fields
                    ],
                    document_id,
                ],
            )

            connection.commit()

        finally:

            connection.close()


    def mark_error(
        document_id,
        error,
    ):

        connection = connect()

        try:

            columns = (
                document_columns(
                    connection
                )
            )

            updates = {}

            if (
                "ocr_status"
                in columns
            ):

                updates[
                    "ocr_status"
                ] = "error"

            if (
                "validation_status"
                in columns
            ):

                updates[
                    "validation_status"
                ] = "warning"

            if (
                "validation_message"
                in columns
            ):

                updates[
                    "validation_message"
                ] = (
                    "Ошибка "
                    "Registration OCR: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

            if updates:

                assignments = (
                    ", ".join(
                        f"{field} = ?"
                        for field
                        in updates
                    )
                )

                connection.execute(
                    f"""
                    UPDATE documents
                    SET {assignments}
                    WHERE id = ?
                    """,
                    [
                        *updates.values(),
                        document_id,
                    ],
                )

                connection.commit()

        finally:

            connection.close()


    def process_document(
        document_id,
    ):

        (
            document,
            student,
        ) = (
            get_document_and_student(
                document_id
            )
        )

        if (
            document[
                "document_type"
            ]
            != DOCUMENT_TYPE
        ):

            return None

        path = (
            upload_dir
            / document[
                "stored_name"
            ]
        )

        if not path.exists():

            raise RuntimeError(
                "Файл документа "
                "не найден."
            )

        result = (
            recognize_registration(
                path,
                student,
            )
        )

        save_result(
            document_id,
            result,
        )

        return result

    # ========================================================
    # После загрузки документа
    # ========================================================

    original_upload = (
        app.view_functions.get(
            "upload_document"
        )
    )

    if original_upload:

        def upload_document_v68(
            student_id,
            document_type,
        ):

            response = (
                original_upload(
                    student_id,
                    document_type,
                )
            )

            if (
                request.method
                == "POST"

                and document_type
                == DOCUMENT_TYPE
            ):

                connection = (
                    connect()
                )

                try:

                    latest = (
                        connection.execute(
                            """
                            SELECT id
                            FROM documents

                            WHERE
                                student_id = ?
                                AND
                                document_type = ?

                            ORDER BY
                                version DESC,
                                id DESC

                            LIMIT 1
                            """,
                            (
                                student_id,
                                DOCUMENT_TYPE,
                            ),
                        ).fetchone()
                    )

                finally:

                    connection.close()

                if latest:

                    try:

                        process_document(
                            latest[
                                "id"
                            ]
                        )

                    except Exception as error:

                        mark_error(
                            latest[
                                "id"
                            ],
                            error,
                        )

                        print(
                            "Registration OCR "
                            "v6.8:",
                            repr(
                                error
                            ),
                        )

            return response

        app.view_functions[
            "upload_document"
        ] = (
            upload_document_v68
        )

    # ========================================================
    # Повторная проверка
    # ========================================================

    original_recheck = (
        app.view_functions.get(
            "validation_recheck_v6"
        )
    )

    if original_recheck:

        def recheck_v68(
            document_id,
        ):

            (
                document,
                _,
            ) = (
                get_document_and_student(
                    document_id
                )
            )

            if (
                document[
                    "document_type"
                ]
                != DOCUMENT_TYPE
            ):

                # Паспорт, СНИЛС,
                # свидетельство о рождении
                # и остальные документы
                # уходят в существующие
                # обработчики.
                return (
                    original_recheck(
                        document_id=
                            document_id
                    )
                )

            try:

                result = (
                    process_document(
                        document_id
                    )
                )

                if (
                    result[
                        "validation_status"
                    ]
                    == "passed"
                ):

                    flash(
                        (
                            "Подтверждение "
                            "регистрации ребенка "
                            "распознано "
                            "и проверено."
                        ),
                        "success",
                    )

                elif (
                    result[
                        "validation_status"
                    ]
                    == "mismatch"
                ):

                    flash(
                        (
                            "Документ распознан, "
                            "но обнаружено "
                            "несовпадение."
                        ),
                        "error",
                    )

                else:

                    flash(
                        (
                            "Документ распознан, "
                            "но часть данных "
                            "требует ручной "
                            "проверки."
                        ),
                        "info",
                    )

            except Exception as error:

                mark_error(
                    document_id,
                    error,
                )

                flash(
                    (
                        "Ошибка "
                        "Registration OCR: "
                        f"{type(error).__name__}: "
                        f"{error}"
                    ),
                    "error",
                )

            if (
                "validation_result_v6"
                in app.view_functions
            ):

                return redirect(
                    url_for(
                        "validation_result_v6",
                        document_id=
                            document_id,
                    )
                )

            return redirect(
                url_for(
                    "student_detail",
                    student_id=
                        document[
                            "student_id"
                        ],
                )
            )

        app.view_functions[
            "validation_recheck_v6"
        ] = recheck_v68

    print(
        "Registration Smart OCR "
        "v6.8 подключен."
    )
