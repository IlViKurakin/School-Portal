from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import fitz
from flask import flash, redirect, request, url_for
from PIL import Image, ImageOps


PASSPORT_TYPES = {
    "parent_passport",
    "child_passport",
}


def apply_passport_ocr_v6_7(
    app,
    namespace: dict[str, Any],
) -> None:

    if app.extensions.get(
        "passport_ocr_v6_7"
    ):
        return

    app.extensions[
        "passport_ocr_v6_7"
    ] = True

    database_path: Path = namespace[
        "DATABASE_PATH"
    ]

    upload_dir: Path = namespace[
        "UPLOAD_DIR"
    ]

    _engine = None
    _engine_error = None

    # =====================================================
    # БАЗА
    # =====================================================

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

    # =====================================================
    # PADDLE OCR
    # =====================================================

    def get_engine():

        nonlocal _engine
        nonlocal _engine_error

        if _engine is not None:
            return _engine

        if _engine_error is not None:
            raise RuntimeError(
                _engine_error
            )

        try:
            from paddleocr import (
                PaddleOCR
            )

            # Используем тот же
            # безопасный режим Windows,
            # что уже работает
            # в текущем портале.
            _engine = PaddleOCR(
                lang="ru",
                ocr_version="PP-OCRv5",

                text_detection_model_name=(
                    "PP-OCRv5_mobile_det"
                ),

                text_recognition_model_name=(
                    "eslav_PP-OCRv5_mobile_rec"
                ),

                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,

                device="cpu",
                engine="paddle_static",
                enable_mkldnn=False,
                cpu_threads=2,
            )

            return _engine

        except Exception as error:

            _engine_error = (
                "Не удалось запустить "
                "PaddleOCR: "
                f"{type(error).__name__}: "
                f"{error!r}"
            )

            raise RuntimeError(
                _engine_error
            ) from error

    def result_to_dict(
        result_item,
    ):
        data = result_item

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
            isinstance(data, dict)
            and isinstance(
                data.get("res"),
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

    def to_list(value):

        if value is None:
            return []

        if hasattr(
            value,
            "tolist",
        ):
            return value.tolist()

        if isinstance(
            value,
            (list, tuple),
        ):
            return list(value)

        return []

    def run_ocr(
        image: Image.Image,
    ):

        image = (
            ImageOps.exif_transpose(
                image
            )
            .convert("RGB")
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
            ) as temp_file:

                temp_path = (
                    temp_file.name
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

            for result_item in prediction:

                data = result_to_dict(
                    result_item
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
                    enumerate(texts)
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
                                0.0,
                                0.0,
                                0.0,
                                0.0,
                            )

                    else:
                        (
                            x1,
                            y1,
                            x2,
                            y2,
                        ) = (
                            0.0,
                            0.0,
                            0.0,
                            0.0,
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

    # =====================================================
    # PDF → ИЗОБРАЖЕНИЯ
    # =====================================================

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
                result = []

                # Для паспорта обычно
                # достаточно первых
                # трех страниц.
                for page_index in range(
                    min(
                        len(document),
                        3,
                    )
                ):

                    page = (
                        document.load_page(
                            page_index
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

                    result.append(
                        image
                    )

                return result

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

    # =====================================================
    # НОРМАЛИЗАЦИЯ
    # =====================================================

    def normalized_name(
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

        return re.sub(
            r"[^а-яa-z-]+",
            "",
            value,
        )

    def normalized_text(
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

        return re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

    def compact(
        value,
    ):

        return re.sub(
            r"[^а-яa-z0-9]+",
            "",
            normalized_text(
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

    def date_to_iso(
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
            return str(
                value
            )

    def box_center(block):

        (
            x1,
            y1,
            x2,
            y2,
        ) = block["box"]

        return (
            (
                x1 + x2
            ) / 2,
            (
                y1 + y2
            ) / 2,
        )

    def box_height(block):

        return max(
            1.0,
            (
                block[
                    "box"
                ][3]
                -
                block[
                    "box"
                ][1]
            ),
        )

    # =====================================================
    # ОРИЕНТАЦИЯ
    # =====================================================

    def passport_layout_score(
        blocks,
    ):

        joined = " ".join(
            block["text"]
            for block in blocks
        )

        text = normalized_text(
            joined
        )

        flat = compact(
            joined
        )

        score = 0

        for (
            marker,
            weight,
        ) in (
            (
                "российская",
                2,
            ),
            (
                "федерация",
                2,
            ),
            (
                "паспорт",
                2,
            ),
            (
                "фамилия",
                4,
            ),
            (
                "отчество",
                3,
            ),
            (
                "рождения",
                3,
            ),
            (
                "пол",
                1,
            ),
        ):

            if marker in text:
                score += weight

        if (
            "датарождения"
            in flat
        ):
            score += 3

        if re.search(
            r"\b\d{2}"
            r"[./-]\d{2}"
            r"[./-]\d{4}\b",
            joined,
        ):
            score += 2

        return score

    def best_orientation(
        image,
    ):

        attempts = []

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
                passport_layout_score(
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

            # Если паспорт и так
            # расположен нормально,
            # не тратим время
            # на еще 3 OCR.
            if (
                angle == 0
                and score >= 10
            ):
                break

            if score >= 15:
                break

        attempts.sort(
            key=lambda item:
                item["score"],
            reverse=True,
        )

        return attempts[0]

    # =====================================================
    # ФИО
    # =====================================================

    LABEL_WORDS = {
        "фамилия",
        "имя",
        "отчество",
        "пол",
        "дата",
        "рождения",
        "место",
        "паспорт",
        "выдан",
    }

    def looks_like_name(
        value,
    ):

        cleaned = re.sub(
            r"[^А-Яа-яЁё-]",
            "",
            str(
                value or ""
            ),
        )

        if len(cleaned) < 2:
            return False

        return (
            normalized_name(
                cleaned
            )
            not in LABEL_WORDS
        )

    def label_similarity(
        value,
        label,
    ):

        return SequenceMatcher(
            None,
            compact(value),
            compact(label),
        ).ratio()

    def find_label_blocks(
        blocks,
        label,
    ):

        matches = []

        for block in blocks:

            similarity = (
                label_similarity(
                    block["text"],
                    label,
                )
            )

            if similarity >= 0.72:
                matches.append(
                    (
                        similarity,
                        block,
                    )
                )

        matches.sort(
            key=lambda item:
                item[0],
            reverse=True,
        )

        return [
            item[1]
            for item in matches
        ]

    def extract_name_field(
        blocks,
        label,
        expected="",
    ):

        candidates = []

        for label_block in (
            find_label_blocks(
                blocks,
                label,
            )
        ):

            (
                lx1,
                _,
                lx2,
                _,
            ) = label_block[
                "box"
            ]

            (
                _,
                label_cy,
            ) = box_center(
                label_block
            )

            label_h = box_height(
                label_block
            )

            for candidate in blocks:

                if (
                    candidate
                    is label_block
                ):
                    continue

                if not looks_like_name(
                    candidate[
                        "text"
                    ]
                ):
                    continue

                (
                    _,
                    cy,
                ) = box_center(
                    candidate
                )

                dy = abs(
                    cy
                    - label_cy
                )

                candidate_h = (
                    box_height(
                        candidate
                    )
                )

                # Значение должно
                # находиться примерно
                # справа от подписи.
                if (
                    candidate[
                        "box"
                    ][0]
                    < lx1 - 10
                ):
                    continue

                if (
                    dy
                    > max(
                        label_h,
                        candidate_h,
                    )
                    * 2.2
                ):
                    continue

                horizontal_gap = max(
                    0,
                    (
                        candidate[
                            "box"
                        ][0]
                        - lx2
                    ),
                )

                score = (
                    100
                    - dy * 3
                    - horizontal_gap
                    * 0.06
                    + float(
                        candidate[
                            "score"
                        ]
                    )
                    * 20
                )

                if expected:

                    similarity = (
                        SequenceMatcher(
                            None,

                            normalized_name(
                                candidate[
                                    "text"
                                ]
                            ),

                            normalized_name(
                                expected
                            ),
                        ).ratio()
                    )

                    score += (
                        similarity
                        * 80
                    )

                candidates.append(
                    (
                        score,
                        candidate[
                            "text"
                        ],
                        float(
                            candidate[
                                "score"
                            ]
                        ),
                    )
                )

        if candidates:

            candidates.sort(
                key=lambda item:
                    item[0],
                reverse=True,
            )

            (
                _,
                value,
                confidence,
            ) = candidates[0]

            cleaned = re.sub(
                r"[^А-Яа-яЁё-]",
                "",
                value,
            )

            return (
                smart_title(
                    cleaned
                ),
                confidence,
            )

        # Если подпись OCR
        # прочитал плохо,
        # пробуем найти ожидаемое
        # ФИО из карточки среди
        # распознанных блоков.
        if expected:

            best = None

            for block in blocks:

                if not looks_like_name(
                    block["text"]
                ):
                    continue

                similarity = (
                    SequenceMatcher(
                        None,

                        normalized_name(
                            block[
                                "text"
                            ]
                        ),

                        normalized_name(
                            expected
                        ),
                    ).ratio()
                )

                if (
                    best is None
                    or similarity
                    > best[0]
                ):
                    best = (
                        similarity,
                        block,
                    )

            if (
                best
                and best[0] >= 0.78
            ):

                cleaned = re.sub(
                    r"[^А-Яа-яЁё-]",
                    "",
                    best[1][
                        "text"
                    ],
                )

                return (
                    smart_title(
                        cleaned
                    ),
                    float(
                        best[1][
                            "score"
                        ]
                    ),
                )

        return (
            "",
            0.0,
        )

    # =====================================================
    # ДАТА РОЖДЕНИЯ
    # =====================================================

    def extract_birth_date(
        blocks,
        expected="",
    ):

        dates = []

        for block in blocks:

            value = date_to_iso(
                block[
                    "text"
                ]
            )

            if value:
                dates.append(
                    (
                        value,
                        block,
                    )
                )

        if not dates:
            return ""

        birth_labels = (
            find_label_blocks(
                blocks,
                "рождения",
            )
            +
            find_label_blocks(
                blocks,
                "дата рождения",
            )
        )

        page_bottom = max(
            (
                block[
                    "box"
                ][3]
                for block in blocks
            ),
            default=1,
        )

        ranked = []

        for (
            value,
            date_block,
        ) in dates:

            score = float(
                date_block[
                    "score"
                ]
            ) * 20

            (
                _,
                date_cy,
            ) = box_center(
                date_block
            )

            # Дата рождения,
            # в отличие от даты выдачи,
            # обычно находится
            # на странице с фото.
            if (
                date_cy
                > page_bottom
                * 0.5
            ):
                score += 25

            for label_block in (
                birth_labels
            ):

                (
                    _,
                    label_cy,
                ) = box_center(
                    label_block
                )

                dy = abs(
                    date_cy
                    - label_cy
                )

                score += max(
                    0,
                    60
                    - dy * 0.25,
                )

            if (
                expected
                and value
                == expected
            ):
                score += 100

            ranked.append(
                (
                    score,
                    value,
                )
            )

        ranked.sort(
            reverse=True,
        )

        return ranked[0][1]

    # =====================================================
    # ВЕРТИКАЛЬНЫЙ НОМЕР
    # =====================================================

    DIGIT_TRANSLATION = (
        str.maketrans(
            {
                "О": "0",
                "O": "0",
                "о": "0",
                "o": "0",
                "I": "1",
                "l": "1",
                "|": "1",
            }
        )
    )

    def number_candidates(
        blocks,
    ):

        texts = [
            str(
                block["text"]
            ).translate(
                DIGIT_TRANSLATION
            )

            for block in blocks
        ]

        result = []

        for source in (
            " ".join(texts),
            " ".join(
                reversed(texts)
            ),
        ):

            for match in re.finditer(
                r"(?<!\d)"
                r"(\d{2})"
                r"\D{0,5}"
                r"(\d{2})"
                r"\D{0,8}"
                r"(\d{6})"
                r"(?!\d)",
                source,
            ):

                result.append(
                    "".join(
                        match.groups()
                    )
                )

            digits = re.sub(
                r"\D",
                "",
                source,
            )

            if len(digits) == 10:
                result.append(
                    digits
                )

            # В паспорте серия
            # и номер часто повторяются
            # на двух страницах.
            if (
                len(digits) >= 20
                and len(digits)
                % 10 == 0
            ):

                for index in range(
                    0,
                    len(digits),
                    10,
                ):
                    result.append(
                        digits[
                            index:
                            index + 10
                        ]
                    )

        return [
            value
            for value in result
            if len(value) == 10
        ]

    def extract_passport_number(
        image,
        expected_series="",
        expected_number="",
    ):

        width, height = (
            image.size
        )

        # Берем правый край,
        # где физически напечатаны
        # серия и номер паспорта.
        x1 = int(
            width * 0.76
        )

        regions = [
            image.crop(
                (
                    x1,
                    0,
                    width,
                    height,
                )
            ),

            image.crop(
                (
                    x1,
                    0,
                    width,
                    int(
                        height
                        * 0.55
                    ),
                )
            ),

            image.crop(
                (
                    x1,
                    int(
                        height
                        * 0.43
                    ),
                    width,
                    height,
                )
            ),
        ]

        found = []

        for region in regions:

            # Цифры вертикальные,
            # поэтому пробуем
            # оба направления.
            for angle in (
                90,
                270,
            ):

                variant = rotated(
                    region,
                    angle,
                )

                blocks = run_ocr(
                    variant
                )

                found.extend(
                    number_candidates(
                        blocks
                    )
                )

        expected = (
            re.sub(
                r"\D",
                "",
                expected_series,
            )
            +
            re.sub(
                r"\D",
                "",
                expected_number,
            )
        )

        counts = Counter(
            found
        )

        if (
            len(expected) == 10
            and expected in counts
        ):
            selected = expected

        elif counts:
            selected = (
                counts.most_common(
                    1
                )[0][0]
            )

        else:
            selected = ""

        return (
            (
                selected[:4]
                if len(selected)
                == 10
                else ""
            ),
            (
                selected[4:]
                if len(selected)
                == 10
                else ""
            ),
            (
                counts.get(
                    selected,
                    0,
                )
                if selected
                else 0
            ),
        )

    # =====================================================
    # ПРОВЕРКА
    # =====================================================

    def compare_name(
        label,
        recognized,
        expected,
        confidence,
        checks,
    ):

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
            SequenceMatcher(
                None,
                normalized_name(
                    recognized
                ),
                normalized_name(
                    expected
                ),
            ).ratio()
            if expected
            else 1
        )

        if similarity >= 0.96:

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        f"{label} "
                        "совпадает: "
                        f"{recognized}.",
                }
            )

            return (
                "ok",
                True,
            )

        # Небольшая OCR-ошибка
        # не должна сразу давать
        # красный результат.
        if similarity >= 0.78:

            checks.append(
                {
                    "level":
                        "yellow",

                    "text":
                        f"{label} "
                        "распознано как "
                        f"«{recognized}». "
                        "Нужна ручная "
                        "проверка.",
                }
            )

            return (
                "warning",
                False,
            )

        if confidence >= 0.70:

            checks.append(
                {
                    "level":
                        "red",

                    "text":
                        f"{label} "
                        "не совпадает. "
                        "В паспорте: "
                        f"«{recognized}», "
                        "в карточке: "
                        f"«{expected}».",
                }
            )

            return (
                "mismatch",
                False,
            )

        checks.append(
            {
                "level":
                    "yellow",

                "text":
                    f"{label} "
                    "распознано "
                    "неуверенно.",
            }
        )

        return (
            "warning",
            False,
        )

    def recognize_passport(
        path,
        document_type,
        student,
    ):

        if (
            document_type
            == "parent_passport"
        ):

            expected = {
                "surname":
                    student.get(
                        "parent_last_name"
                    ) or "",

                "first_name":
                    student.get(
                        "parent_first_name"
                    ) or "",

                "middle_name":
                    student.get(
                        "parent_middle_name"
                    ) or "",

                "birth_date":
                    student.get(
                        "parent_birth_date"
                    ) or "",

                "series":
                    student.get(
                        "parent_passport_series"
                    ) or "",

                "number":
                    student.get(
                        "parent_passport_number"
                    ) or "",
            }

            fields = {
                "surname":
                    "parent_last_name",

                "first_name":
                    "parent_first_name",

                "middle_name":
                    "parent_middle_name",

                "birth_date":
                    "parent_birth_date",

                "series":
                    "parent_passport_series",

                "number":
                    "parent_passport_number",
            }

        else:

            expected = {
                "surname":
                    student.get(
                        "last_name"
                    ) or "",

                "first_name":
                    student.get(
                        "first_name"
                    ) or "",

                "middle_name":
                    student.get(
                        "middle_name"
                    ) or "",

                "birth_date":
                    student.get(
                        "birth_date"
                    ) or "",

                "series":
                    student.get(
                        "child_passport_series"
                    ) or "",

                "number":
                    student.get(
                        "child_passport_number"
                    ) or "",
            }

            fields = {
                "surname":
                    "last_name",

                "first_name":
                    "first_name",

                "middle_name":
                    "middle_name",

                "birth_date":
                    "birth_date",

                "series":
                    "child_passport_series",

                "number":
                    "child_passport_number",
            }

        pages = load_pages(
            path
        )

        page_results = []

        for page_index, page in (
            enumerate(pages)
        ):

            result = (
                best_orientation(
                    page
                )
            )

            result[
                "page_index"
            ] = page_index

            page_results.append(
                result
            )

        page_results.sort(
            key=lambda item:
                item["score"],
            reverse=True,
        )

        best = page_results[0]

        blocks = best[
            "blocks"
        ]

        surname, surname_score = (
            extract_name_field(
                blocks,
                "фамилия",
                expected[
                    "surname"
                ],
            )
        )

        first_name, first_score = (
            extract_name_field(
                blocks,
                "имя",
                expected[
                    "first_name"
                ],
            )
        )

        (
            middle_name,
            middle_score,
        ) = extract_name_field(
            blocks,
            "отчество",
            expected[
                "middle_name"
            ],
        )

        birth_date = (
            extract_birth_date(
                blocks,
                expected[
                    "birth_date"
                ],
            )
        )

        (
            series,
            number,
            repeat_count,
        ) = extract_passport_number(
            best["image"],
            expected[
                "series"
            ],
            expected[
                "number"
            ],
        )

        checks = [
            {
                "level":
                    "green"
                    if best["score"]
                    >= 5
                    else "yellow",

                "text":
                    (
                        "Тип документа "
                        "соответствует "
                        "ожидаемому: паспорт."
                        if best["score"]
                        >= 5

                        else
                        "Тип паспорта "
                        "определен "
                        "неуверенно."
                    ),
            }
        ]

        states = []
        matched = 0

        for (
            label,
            recognized,
            exp,
            confidence,
        ) in (
            (
                "Фамилия",
                surname,
                expected[
                    "surname"
                ],
                surname_score,
            ),
            (
                "Имя",
                first_name,
                expected[
                    "first_name"
                ],
                first_score,
            ),
            (
                "Отчество",
                middle_name,
                expected[
                    "middle_name"
                ],
                middle_score,
            ),
        ):

            state, is_match = (
                compare_name(
                    label,
                    recognized,
                    exp,
                    confidence,
                    checks,
                )
            )

            states.append(
                state
            )

            matched += int(
                is_match
            )

        if birth_date:

            if (
                expected[
                    "birth_date"
                ]
                and birth_date
                != expected[
                    "birth_date"
                ]
            ):

                states.append(
                    "mismatch"
                )

                checks.append(
                    {
                        "level":
                            "red",

                        "text":
                            "Дата рождения "
                            "не совпадает. "
                            f"Паспорт: "
                            f"{display_date(birth_date)}, "
                            "карточка: "
                            f"{display_date(expected['birth_date'])}.",
                    }
                )

            else:

                states.append(
                    "ok"
                )

                matched += 1

                checks.append(
                    {
                        "level":
                            "green",

                        "text":
                            "Дата рождения "
                            "совпадает: "
                            f"{display_date(birth_date)}.",
                    }
                )

        else:
            states.append(
                "warning"
            )

            checks.append(
                {
                    "level":
                        "yellow",

                    "text":
                        "Дата рождения "
                        "не распознана.",
                }
            )

        for (
            label,
            recognized,
            exp,
        ) in (
            (
                "Серия паспорта",
                series,
                expected[
                    "series"
                ],
            ),
            (
                "Номер паспорта",
                number,
                expected[
                    "number"
                ],
            ),
        ):

            if not recognized:

                states.append(
                    "warning"
                )

                checks.append(
                    {
                        "level":
                            "yellow",

                        "text":
                            f"{label} "
                            "не распознано.",
                    }
                )

            elif (
                exp
                and re.sub(
                    r"\D",
                    "",
                    recognized,
                )
                != re.sub(
                    r"\D",
                    "",
                    exp,
                )
            ):

                states.append(
                    "mismatch"
                )

                checks.append(
                    {
                        "level":
                            "red",

                        "text":
                            f"{label} "
                            "не совпадает. "
                            f"Паспорт: "
                            f"{recognized}, "
                            f"карточка: "
                            f"{exp}.",
                    }
                )

            else:

                states.append(
                    "ok"
                )

                checks.append(
                    {
                        "level":
                            "green",

                        "text":
                            f"{label}: "
                            f"{recognized}.",
                    }
                )

        if (
            repeat_count >= 2
            and series
            and number
        ):

            checks.append(
                {
                    "level":
                        "green",

                    "text":
                        "Серия и номер "
                        "найдены повторно "
                        "на развороте.",
                }
            )

        if (
            "mismatch"
            in states
        ):
            status = (
                "mismatch"
            )

        elif (
            matched >= 3
            and series
            and number
        ):
            status = (
                "passed"
            )

        else:
            status = (
                "warning"
            )

        extracted = {
            "surname":
                surname,

            "first_name":
                first_name,

            "middle_name":
                middle_name,

            "birth_date":
                birth_date,

            "series":
                series,

            "number":
                number,
        }

        candidates = {
            fields[key]:
                value

            for key, value
            in extracted.items()

            if value
        }

        passport = {
            **extracted,

            "orientation":
                best["angle"],

            "page_index":
                best[
                    "page_index"
                ],

            "layout_score":
                best["score"],
        }

        confidence = (
            0.98
            if best["score"]
            >= 12

            else 0.90
            if best["score"]
            >= 8

            else 0.78
            if best["score"]
            >= 5

            else 0.55
        )

        return {
            "ocr_text":
                "\n".join(
                    block["text"]
                    for block
                    in blocks
                ),

            "ocr_json": {
                "engine":
                    "Passport Smart "
                    "OCR v6.7",

                "entities": {
                    "surname":
                        surname,

                    "name":
                        first_name,

                    "middle_name":
                        middle_name,

                    "birth_date":
                        birth_date,

                    # Совместимость
                    # с существующим
                    # модулем аттестации.
                    "number":
                        (
                            series
                            + number
                            if (
                                series
                                and number
                            )
                            else ""
                        ),

                    "passport_series":
                        series,

                    "passport_number":
                        number,
                },

                "passport":
                    passport,

                # Сохраняем также
                # координаты OCR.
                "blocks":
                    blocks,
            },

            "validation_status":
                status,

            "detected_document_type":
                "passport",

            "validation_confidence":
                confidence,

            "validation_message":
                (
                    "Паспорт распознан "
                    "по расположению полей."
                    if status
                    == "passed"

                    else
                    "Паспорт распознан. "
                    "Требуется проверка "
                    "результата."
                ),

            "validation_details": {
                "expected":
                    document_type,

                "detected":
                    "passport",

                "confidence":
                    confidence,

                "checks":
                    checks,

                # Именно отсюда
                # существующая логика
                # сохранения сможет
                # взять реквизиты.
                "candidates":
                    candidates,

                "passport":
                    passport,
            },
        }

    # =====================================================
    # СОХРАНЕНИЕ РЕЗУЛЬТАТА
    # =====================================================

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

                "autofill_blocked":
                    (
                        0
                        if result[
                            "validation_status"
                        ]
                        == "passed"
                        else 1
                    ),

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
                if field in columns
            ]

            assignments = ", ".join(
                f"{field} = ?"
                for field in fields
            )

            connection.execute(
                f"""
                UPDATE documents
                SET {assignments}
                WHERE id = ?
                """,
                [
                    *[
                        values[field]
                        for field
                        in fields
                    ],
                    document_id,
                ],
            )

            connection.commit()

        finally:
            connection.close()

    def process_passport(
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
            not in PASSPORT_TYPES
        ):
            return None

        path = (
            upload_dir
            / document[
                "stored_name"
            ]
        )

        result = recognize_passport(
            path,
            document[
                "document_type"
            ],
            student,
        )

        save_result(
            document_id,
            result,
        )

        return result

    # =====================================================
    # АВТОПРОВЕРКА ПОСЛЕ ЗАГРУЗКИ
    # =====================================================

    original_upload = (
        app.view_functions.get(
            "upload_document"
        )
    )

    if original_upload:

        def upload_document_v67(
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
                in PASSPORT_TYPES
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
                                document_type,
                            ),
                        ).fetchone()
                    )

                finally:
                    connection.close()

                if latest:

                    try:
                        process_passport(
                            latest["id"]
                        )

                    except Exception as error:
                        print(
                            "Passport OCR "
                            "v6.7:",
                            repr(error),
                        )

            return response

        app.view_functions[
            "upload_document"
        ] = upload_document_v67

    # =====================================================
    # ПОВТОРНАЯ ПРОВЕРКА
    # =====================================================

    original_recheck = (
        app.view_functions.get(
            "validation_recheck_v6"
        )
    )

    if original_recheck:

        def recheck_v67(
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
                not in PASSPORT_TYPES
            ):

                return (
                    original_recheck(
                        document_id=
                            document_id
                    )
                )

            try:

                result = (
                    process_passport(
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
                        "Паспорт распознан "
                        "и проверен.",
                        "success",
                    )

                else:

                    flash(
                        "Паспорт распознан, "
                        "но часть данных "
                        "требует проверки.",
                        "info",
                    )

            except Exception as error:

                flash(
                    (
                        "Ошибка Passport OCR: "
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
        ] = recheck_v67

    print(
        "Passport Smart OCR "
        "v6.7 подключен."
    )