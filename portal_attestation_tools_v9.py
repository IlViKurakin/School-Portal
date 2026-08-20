
from __future__ import annotations

import io
import json
import re
import sqlite3
import uuid

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    abort,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter


# ============================================================
# Поля, которые аттестация может проверить/исправить
# ============================================================

DOCUMENT_FIELDS = {

    "parent_snils": [
        ("parent_snils", "СНИЛС законного представителя", "text"),
    ],

    "child_snils": [
        ("student_snils", "СНИЛС ребенка", "text"),
    ],

    "birth_certificate": [
        ("last_name", "Фамилия ребенка", "text"),
        ("first_name", "Имя ребенка", "text"),
        ("middle_name", "Отчество ребенка", "text"),
        ("birth_date", "Дата рождения", "date"),
        (
            "birth_certificate_series",
            "Серия свидетельства",
            "text",
        ),
        (
            "birth_certificate_number",
            "Номер свидетельства",
            "text",
        ),
    ],

    "parent_passport": [
        (
            "parent_last_name",
            "Фамилия законного представителя",
            "text",
        ),
        (
            "parent_first_name",
            "Имя законного представителя",
            "text",
        ),
        (
            "parent_middle_name",
            "Отчество законного представителя",
            "text",
        ),
        (
            "parent_birth_date",
            "Дата рождения",
            "date",
        ),
        (
            "parent_gender",
            "Пол",
            "gender",
        ),
        (
            "parent_passport_series",
            "Серия паспорта",
            "text",
        ),
        (
            "parent_passport_number",
            "Номер паспорта",
            "text",
        ),
    ],

    "child_passport": [
        ("last_name", "Фамилия ребенка", "text"),
        ("first_name", "Имя ребенка", "text"),
        ("middle_name", "Отчество ребенка", "text"),
        ("birth_date", "Дата рождения", "date"),
        ("gender", "Пол", "gender"),
        (
            "child_passport_series",
            "Серия паспорта",
            "text",
        ),
        (
            "child_passport_number",
            "Номер паспорта",
            "text",
        ),
    ],
}


NOT_NULL_FIELDS = {
    "last_name",
    "first_name",
    "birth_date",
    "parent_last_name",
    "parent_first_name",
}


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
}

MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_FILES = 30


def apply_attestation_tools_v9(
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
    audit = namespace["audit"]

    document_types = namespace["DOCUMENT_TYPES"]

    # ========================================================
    # Миграция
    # ========================================================

    def migrate_database() -> None:

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

            migrations = {
                "manual_data_json":
                    "ALTER TABLE documents "
                    "ADD COLUMN manual_data_json TEXT",

                "manual_data_updated_by":
                    "ALTER TABLE documents "
                    "ADD COLUMN manual_data_updated_by INTEGER",

                "manual_data_updated_at":
                    "ALTER TABLE documents "
                    "ADD COLUMN manual_data_updated_at TEXT",

                "attestation_verified":
                    "ALTER TABLE documents "
                    "ADD COLUMN attestation_verified "
                    "INTEGER NOT NULL DEFAULT 0",

                "attestation_verified_by":
                    "ALTER TABLE documents "
                    "ADD COLUMN attestation_verified_by INTEGER",

                "attestation_verified_at":
                    "ALTER TABLE documents "
                    "ADD COLUMN attestation_verified_at TEXT",

                "replaced_by_attestation":
                    "ALTER TABLE documents "
                    "ADD COLUMN replaced_by_attestation "
                    "INTEGER NOT NULL DEFAULT 0",
            }

            for column, sql in migrations.items():

                if column not in columns:
                    connection.execute(sql)

            connection.commit()

        finally:
            connection.close()

    migrate_database()

    # ========================================================
    # Общие функции
    # ========================================================

    def document_columns():
        return {
            row[1]
            for row in get_db().execute(
                "PRAGMA table_info(documents)"
            ).fetchall()
        }

    def can_edit(student) -> bool:

        if g.current_user["role"] == "admin":
            return True

        return (
            student["status"] == "in_review"
            and student["assigned_to"]
            == g.current_user["id"]
        )

    def require_edit(student) -> None:

        if can_edit(student):
            return

        abort(
            403,
            (
                "Для изменения документа "
                "сначала возьмите карточку "
                "в работу."
            ),
        )

    def parse_json(value):

        try:
            result = json.loads(
                value or "{}"
            )

            return (
                result
                if isinstance(result, dict)
                else {}
            )

        except Exception:
            return {}

    def format_date(value):

        if not value:
            return "—"

        try:
            return datetime.strptime(
                value,
                "%Y-%m-%d",
            ).strftime(
                "%d.%m.%Y"
            )

        except ValueError:
            return str(value)

    def display_value(field, value):

        if value in (
            None,
            "",
        ):
            return "—"

        if "date" in field:
            return format_date(
                str(value)
            )

        return str(value)

    def format_snils(value: str) -> str:

        digits = re.sub(
            r"\D",
            "",
            value or "",
        )

        if len(digits) != 11:
            return value.strip()

        return (
            f"{digits[:3]}-"
            f"{digits[3:6]}-"
            f"{digits[6:9]} "
            f"{digits[9:]}"
        )

    # ========================================================
    # Что именно распознал OCR
    # ========================================================

    def recognized_values(document):

        result = {}

        details = parse_json(
            (
                document[
                    "validation_details"
                ]
                if "validation_details"
                in document.keys()
                else None
            )
        )

        candidates = details.get(
            "candidates",
            {},
        )

        if isinstance(
            candidates,
            dict,
        ):
            result.update(
                {
                    str(key): value
                    for key, value
                    in candidates.items()
                    if value not in (
                        None,
                        "",
                    )
                }
            )

        ocr_json = parse_json(
            document["ocr_json"]
            if "ocr_json"
            in document.keys()
            else None
        )

        entities = ocr_json.get(
            "entities",
            {},
        )

        if not isinstance(
            entities,
            dict,
        ):
            entities = {}

        document_type = document[
            "document_type"
        ]

        # -----------------------------------
        # Паспорт
        # -----------------------------------

        if document_type in {
            "parent_passport",
            "child_passport",
        }:

            number = re.sub(
                r"\D",
                "",
                str(
                    entities.get(
                        "number",
                        "",
                    )
                ),
            )

            if len(number) >= 10:

                if (
                    document_type
                    == "parent_passport"
                ):
                    result.setdefault(
                        "parent_passport_series",
                        number[:4],
                    )

                    result.setdefault(
                        "parent_passport_number",
                        number[-6:],
                    )

                else:
                    result.setdefault(
                        "child_passport_series",
                        number[:4],
                    )

                    result.setdefault(
                        "child_passport_number",
                        number[-6:],
                    )

            mapping = (
                {
                    "surname":
                        "parent_last_name",

                    "name":
                        "parent_first_name",

                    "middle_name":
                        "parent_middle_name",

                    "birth_date":
                        "parent_birth_date",

                    "gender":
                        "parent_gender",
                }
                if document_type
                == "parent_passport"

                else
                {
                    "surname":
                        "last_name",

                    "name":
                        "first_name",

                    "middle_name":
                        "middle_name",

                    "birth_date":
                        "birth_date",

                    "gender":
                        "gender",
                }
            )

            for source, target in (
                mapping.items()
            ):

                value = entities.get(
                    source
                )

                if value:
                    result.setdefault(
                        target,
                        value,
                    )

        text = (
            document["ocr_text"]
            if (
                "ocr_text"
                in document.keys()
                and document["ocr_text"]
            )
            else ""
        )

        # -----------------------------------
        # СНИЛС
        # -----------------------------------

        if document_type in {
            "parent_snils",
            "child_snils",
        }:

            match = re.search(
                r"(?<!\d)"
                r"(\d{3})\D{0,3}"
                r"(\d{3})\D{0,3}"
                r"(\d{3})\D{0,3}"
                r"(\d{2})(?!\d)",
                text,
            )

            if match:

                value = format_snils(
                    "".join(
                        match.groups()
                    )
                )

                field = (
                    "parent_snils"
                    if document_type
                    == "parent_snils"
                    else "student_snils"
                )

                result.setdefault(
                    field,
                    value,
                )

        # -----------------------------------
        # Свидетельство
        # -----------------------------------

        if (
            document_type
            == "birth_certificate"
        ):

            match = re.search(
                (
                    r"\b"
                    r"([IVXLCDM1]{1,6})"
                    r"\s*[-–—]\s*"
                    r"([А-ЯЁA-Z]{2})"
                    r"\s*(?:№|N|#)?\s*"
                    r"(\d{6})\b"
                ),
                text.upper(),
            )

            if match:

                series = (
                    match.group(1)
                    .replace(
                        "1",
                        "I",
                    )
                    + "-"
                    + match.group(2)
                )

                result.setdefault(
                    "birth_certificate_series",
                    series,
                )

                result.setdefault(
                    "birth_certificate_number",
                    match.group(3),
                )

            # v6.6 пишет подтвержденную
            # дату в checks.
            checks = details.get(
                "checks",
                [],
            )

            if isinstance(
                checks,
                list,
            ):

                for check in checks:

                    if not isinstance(
                        check,
                        dict,
                    ):
                        continue

                    message = str(
                        check.get(
                            "text",
                            "",
                        )
                    )

                    date_match = re.search(
                        r"Дата рождения "
                        r"совпадает:\s*"
                        r"(\d{2}\.\d{2}\.\d{4})",
                        message,
                    )

                    if date_match:

                        try:
                            iso = (
                                datetime.strptime(
                                    date_match.group(1),
                                    "%d.%m.%Y",
                                )
                                .strftime(
                                    "%Y-%m-%d"
                                )
                            )

                            result.setdefault(
                                "birth_date",
                                iso,
                            )

                        except ValueError:
                            pass

        return result

    # ========================================================
    # Подготовка файлов
    # ========================================================

    def validate_upload(upload):

        filename = (
            Path(
                str(
                    upload.filename
                    or ""
                ).replace(
                    "\\",
                    "/",
                )
            )
            .name
            .strip()
        )

        if not filename:
            raise ValueError(
                "Не указано имя файла."
            )

        extension = (
            Path(filename)
            .suffix
            .lower()
        )

        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError(
                (
                    f"Файл «{filename}»: "
                    "разрешены PDF, JPG, "
                    "JPEG и PNG."
                )
            )

        content = upload.read()

        if not content:
            raise ValueError(
                f"Файл «{filename}» пустой."
            )

        if len(content) > MAX_FILE_SIZE:
            raise ValueError(
                f"Файл «{filename}» "
                "больше 25 МБ."
            )

        pdf_position = content[
            :1024
        ].find(
            b"%PDF"
        )

        if extension == ".pdf":

            if pdf_position < 0:
                raise ValueError(
                    (
                        f"Файл «{filename}» "
                        "не является PDF."
                    )
                )

            content = content[
                pdf_position:
            ]

            reader = PdfReader(
                io.BytesIO(content)
            )

            if reader.is_encrypted:
                raise ValueError(
                    (
                        f"Файл «{filename}» "
                        "защищен паролем."
                    )
                )

            if not reader.pages:
                raise ValueError(
                    (
                        f"Файл «{filename}» "
                        "не содержит страниц."
                    )
                )

            return (
                filename,
                content,
                "pdf",
            )

        image = Image.open(
            io.BytesIO(content)
        )

        image.load()

        return (
            filename,
            content,
            "image",
        )

    def image_reader(content):

        image = Image.open(
            io.BytesIO(content)
        )

        image = ImageOps.exif_transpose(
            image
        )

        if image.mode != "RGB":
            image = image.convert(
                "RGB"
            )

        output = io.BytesIO()

        image.save(
            output,
            format="PDF",
            resolution=150,
        )

        output.seek(0)

        return PdfReader(
            output
        )

    def merge_uploads(uploads):

        if len(uploads) > MAX_FILES:
            raise ValueError(
                "Максимум 30 файлов "
                "за одну загрузку."
            )

        writer = PdfWriter()

        names = []
        page_count = 0

        for upload in uploads:

            (
                filename,
                content,
                kind,
            ) = validate_upload(
                upload
            )

            names.append(
                filename
            )

            reader = (
                PdfReader(
                    io.BytesIO(
                        content
                    )
                )
                if kind == "pdf"
                else image_reader(
                    content
                )
            )

            for page in reader.pages:
                writer.add_page(
                    page
                )
                page_count += 1

        output = io.BytesIO()

        writer.write(
            output
        )

        return (
            output.getvalue(),
            names,
            page_count,
        )

    # ========================================================
    # Список последних документов
    # ========================================================

    def latest_documents(
        student_id,
    ):

        return get_db().execute(
            """
            SELECT
                d.*,
                u.full_name
                    AS uploader_name
            FROM documents d

            JOIN (
                SELECT
                    document_type,
                    MAX(version)
                        AS latest_version
                FROM documents
                WHERE student_id = ?
                GROUP BY document_type
            ) latest

              ON latest.document_type =
                    d.document_type

             AND latest.latest_version =
                    d.version

            LEFT JOIN users u
              ON u.id = d.uploaded_by

            WHERE d.student_id = ?
            """,
            (
                student_id,
                student_id,
            ),
        ).fetchall()

    # ========================================================
    # Ручное исправление реквизитов
    # ========================================================

    def update_document_data(
        student_id: int,
        document_id: int,
    ):

        student = get_student_or_404(
            student_id
        )

        require_edit(
            student
        )

        document = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
              AND student_id = ?
            """,
            (
                document_id,
                student_id,
            ),
        ).fetchone()

        if not document:
            abort(404)

        latest_version = get_db().execute(
            """
            SELECT MAX(version)
            FROM documents
            WHERE student_id = ?
              AND document_type = ?
            """,
            (
                student_id,
                document[
                    "document_type"
                ],
            ),
        ).fetchone()[0]

        if (
            document["version"]
            != latest_version
        ):
            abort(
                400,
                (
                    "Изменять данные можно "
                    "только у последней "
                    "версии документа."
                ),
            )

        config = DOCUMENT_FIELDS.get(
            document[
                "document_type"
            ],
            [],
        )

        if not config:
            abort(
                400,
                (
                    "Для этого документа "
                    "нет структурированных "
                    "полей OCR."
                ),
            )

        values = {}
        errors = []

        for (
            field,
            label,
            field_type,
        ) in config:

            value = request.form.get(
                f"field_{field}",
                "",
            ).strip()

            if (
                field in NOT_NULL_FIELDS
                and not value
            ):
                errors.append(
                    f"Поле «{label}» "
                    "не может быть пустым."
                )

                continue

            if (
                field_type == "date"
                and value
            ):

                try:
                    datetime.strptime(
                        value,
                        "%Y-%m-%d",
                    )

                except ValueError:
                    errors.append(
                        (
                            f"Некорректная дата "
                            f"в поле «{label}»."
                        )
                    )

            if (
                field_type == "gender"
                and value
                not in (
                    "",
                    "Мужской",
                    "Женский",
                )
            ):
                errors.append(
                    (
                        f"Некорректное значение "
                        f"поля «{label}»."
                    )
                )

            if field in {
                "student_snils",
                "parent_snils",
            } and value:

                digits = re.sub(
                    r"\D",
                    "",
                    value,
                )

                if len(digits) != 11:
                    errors.append(
                        (
                            f"Поле «{label}» "
                            "должно содержать "
                            "11 цифр."
                        )
                    )

                else:
                    value = format_snils(
                        digits
                    )

            if field in {
                "parent_passport_series",
                "child_passport_series",
            } and value:

                digits = re.sub(
                    r"\D",
                    "",
                    value,
                )

                if len(digits) != 4:
                    errors.append(
                        (
                            f"Поле «{label}» "
                            "должно содержать "
                            "4 цифры."
                        )
                    )

                value = digits

            if field in {
                "parent_passport_number",
                "child_passport_number",
            } and value:

                digits = re.sub(
                    r"\D",
                    "",
                    value,
                )

                if len(digits) != 6:
                    errors.append(
                        (
                            f"Поле «{label}» "
                            "должно содержать "
                            "6 цифр."
                        )
                    )

                value = digits

            values[field] = value

        if errors:

            for error in errors:
                flash(
                    error,
                    "error",
                )

            return redirect(
                url_for(
                    "review_student",
                    student_id=student_id,
                )
            )

        before = {
            field:
                student[field]
            for field in values
        }

        assignments = ", ".join(
            f"{field} = ?"
            for field in values
        )

        timestamp = (
            datetime.now()
            .isoformat(
                timespec="seconds"
            )
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
                *values.values(),
                timestamp,
                student_id,
            ],
        )

        columns = document_columns()

        document_updates = {
            "manual_data_json":
                json.dumps(
                    values,
                    ensure_ascii=False,
                ),

            "manual_data_updated_by":
                g.current_user["id"],

            "manual_data_updated_at":
                timestamp,

            "attestation_verified":
                1,

            "attestation_verified_by":
                g.current_user["id"],

            "attestation_verified_at":
                timestamp,
        }

        if (
            "manual_review_required"
            in columns
        ):
            document_updates[
                "manual_review_required"
            ] = 0

        assignments = ", ".join(
            f"{field} = ?"
            for field
            in document_updates
            if field in columns
        )

        used_values = [
            value
            for field, value
            in document_updates.items()
            if field in columns
        ]

        if assignments:

            get_db().execute(
                f"""
                UPDATE documents
                SET {assignments}
                WHERE id = ?
                """,
                [
                    *used_values,
                    document_id,
                ],
            )

        get_db().commit()

        audit(
            "attestation_document_data_updated",
            student_id,
            json.dumps(
                {
                    "document_id":
                        document_id,

                    "document_type":
                        document[
                            "document_type"
                        ],

                    "before":
                        before,

                    "after":
                        values,
                },
                ensure_ascii=False,
            ),
        )

        flash(
            (
                "Данные сохранены. "
                "Документ отмечен как "
                "проверенный сотрудником "
                "аттестации."
            ),
            "success",
        )

        return redirect(
            url_for(
                "review_student",
                student_id=student_id,
            )
        )

    # ========================================================
    # Замена документа сотрудником аттестации
    # ========================================================

    def replace_document(
        student_id: int,
        document_type: str,
    ):

        student = get_student_or_404(
            student_id
        )

        require_edit(
            student
        )

        if (
            document_type
            not in document_types
        ):
            abort(404)

        latest = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE student_id = ?
              AND document_type = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (
                student_id,
                document_type,
            ),
        ).fetchone()

        if request.method == "POST":

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
                    "Выберите файл.",
                    "error",
                )

                return redirect(
                    request.url
                )

            try:

                (
                    pdf,
                    names,
                    page_count,
                ) = merge_uploads(
                    uploads
                )

            except Exception as error:

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
                pdf
            )

            latest_version = (
                get_db().execute(
                    """
                    SELECT COALESCE(
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

            timestamp = (
                datetime.now()
                .isoformat(
                    timespec="seconds"
                )
            )

            columns = document_columns()

            fields = {
                "student_id":
                    student_id,

                "document_type":
                    document_type,

                "original_name":
                    " + ".join(
                        names
                    )[:1500],

                "stored_name":
                    stored_name,

                "mime_type":
                    "application/pdf",

                "version":
                    latest_version + 1,

                "source_document_id":
                    (
                        latest["id"]
                        if latest
                        else None
                    ),

                "uploaded_by":
                    g.current_user["id"],

                "uploaded_at":
                    timestamp,

                "ocr_status":
                    "processing",
            }

            optional = {
                "page_count":
                    page_count,

                "validation_status":
                    "checking",

                "replaced_by_attestation":
                    1,

                "attestation_verified":
                    0,

                "save_confirmed":
                    1,

                "saved_at":
                    timestamp,

                "saved_by":
                    g.current_user["id"],
            }

            for field, value in (
                optional.items()
            ):

                if field in columns:
                    fields[field] = value

            column_list = (
                ", ".join(
                    fields.keys()
                )
            )

            placeholders = (
                ", ".join(
                    "?"
                    for _ in fields
                )
            )

            cursor = get_db().execute(
                f"""
                INSERT INTO documents (
                    {column_list}
                )
                VALUES (
                    {placeholders}
                )
                """,
                list(
                    fields.values()
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
                    timestamp,
                    student_id,
                ),
            )

            get_db().commit()

            audit(
                "attestation_document_replaced",
                student_id,
                (
                    f"document_type="
                    f"{document_type}; "
                    f"new_document_id="
                    f"{document_id}; "
                    f"version="
                    f"{latest_version + 1}"
                ),
            )

            # Прогоняем новый файл через
            # уже существующую v6-проверку.
            recheck = (
                app.view_functions.get(
                    "validation_recheck_v6"
                )
            )

            if recheck:

                try:
                    recheck(
                        document_id=document_id
                    )

                except Exception as error:

                    flash(
                        (
                            "Новая версия сохранена, "
                            "но OCR завершился "
                            f"ошибкой: {error}"
                        ),
                        "info",
                    )

            document_after = (
                get_db().execute(
                    """
                    SELECT *
                    FROM documents
                    WHERE id = ?
                    """,
                    (
                        document_id,
                    ),
                ).fetchone()
            )

            if (
                document_after
                and
                "manual_review_required"
                in columns
            ):

                validation = (
                    document_after[
                        "validation_status"
                    ]
                    if (
                        "validation_status"
                        in document_after.keys()
                    )
                    else ""
                )

                get_db().execute(
                    """
                    UPDATE documents
                    SET manual_review_required = ?
                    WHERE id = ?
                    """,
                    (
                        0
                        if validation
                        == "passed"
                        else 1,

                        document_id,
                    ),
                )

                get_db().commit()

            flash(
                (
                    "Новая версия документа "
                    "загружена сотрудником "
                    "аттестации."
                ),
                "success",
            )

            return redirect(
                url_for(
                    "review_student",
                    student_id=student_id,
                )
            )

        body = """
        <h1>
            Замена документа
        </h1>

        <div class="card">
            <h2 style="margin-top:0">
                {{ document_name }}
            </h2>

            {% if latest %}

                <p>
                    Текущая версия:
                    <strong>
                        {{ latest.version }}
                    </strong>
                </p>

                <p>
                    Новый файл будет сохранен
                    как версия
                    <strong>
                        {{ latest.version + 1 }}
                    </strong>.
                </p>

            {% endif %}

            <div class="alert info">
                Старая версия не удаляется
                и остается в истории.
            </div>

            <form
                method="post"
                enctype="multipart/form-data"
            >
                <input
                    type="hidden"
                    name="csrf_token"
                    value="{{ csrf_token }}"
                >

                <label>
                    Новый файл
                    или несколько страниц
                </label>

                <input
                    type="file"
                    name="files"
                    multiple
                    accept="
                        .pdf,
                        .jpg,
                        .jpeg,
                        .png
                    "
                    required
                >

                <div
                    class="space"
                    style="
                        display:flex;
                        gap:10px;
                        flex-wrap:wrap;
                    "
                >
                    <button
                        class="btn btn-primary"
                        type="submit"
                    >
                        Загрузить новую версию
                    </button>

                    <a
                        class="btn btn-secondary"
                        href="{{ url_for(
                            'review_student',
                            student_id=student.id
                        ) }}"
                    >
                        Назад
                    </a>
                </div>
            </form>
        </div>
        """

        return namespace[
            "render_page"
        ](
            "Замена документа",
            body,
            student=student,
            latest=latest,
            document_name=(
                document_types[
                    document_type
                ]["name"]
            ),
        )

    # ========================================================
    # Рабочий блок аттестации
    # ========================================================

    def review_tools_html(
        student,
    ):

        documents = {
            row["document_type"]: row
            for row
            in latest_documents(
                student["id"]
            )
        }

        items = []

        for code, config in (
            document_types.items()
        ):

            document = documents.get(
                code
            )

            recognized = (
                recognized_values(
                    document
                )
                if document
                else {}
            )

            field_rows = []

            for (
                field,
                label,
                field_type,
            ) in DOCUMENT_FIELDS.get(
                code,
                [],
            ):

                field_rows.append(
                    {
                        "field":
                            field,

                        "label":
                            label,

                        "type":
                            field_type,

                        "recognized":
                            display_value(
                                field,
                                recognized.get(
                                    field
                                ),
                            ),

                        "current":
                            (
                                student[field]
                                or ""
                            ),

                        "current_display":
                            display_value(
                                field,
                                student[field],
                            ),
                    }
                )

            details = (
                parse_json(
                    document[
                        "validation_details"
                    ]
                    if (
                        document
                        and
                        "validation_details"
                        in document.keys()
                    )
                    else None
                )
                if document
                else {}
            )

            items.append(
                {
                    "code":
                        code,

                    "name":
                        config["name"],

                    "document":
                        document,

                    "fields":
                        field_rows,

                    "checks":
                        details.get(
                            "checks",
                            [],
                        ),

                    "ocr_text":
                        (
                            document[
                                "ocr_text"
                            ]
                            if (
                                document
                                and
                                "ocr_text"
                                in document.keys()
                            )
                            else ""
                        ),

                    "validation":
                        (
                            document[
                                "validation_status"
                            ]
                            if (
                                document
                                and
                                "validation_status"
                                in document.keys()
                            )
                            else ""
                        ),

                    "verified":
                        bool(
                            document[
                                "attestation_verified"
                            ]
                            if (
                                document
                                and
                                "attestation_verified"
                                in document.keys()
                            )
                            else 0
                        ),
                }
            )

        template = """
        <h2>
            Проверка документов
            и распознанных данных
        </h2>

        <div class="alert info">
            Здесь показано отдельно:

            <strong>
                что распознал OCR
            </strong>
            и
            <strong>
                что сейчас записано
                в карточке ученика
            </strong>.

            <br><br>

            Сотрудник аттестации может
            открыть оригинал, заменить файл
            новой версией или вручную
            исправить реквизиты.
        </div>

        {% if not can_edit %}
            <div class="alert info">
                Для внесения изменений
                сначала нажмите
                <strong>
                    «Взять в работу»
                </strong>.
            </div>
        {% endif %}

        {% for item in items %}

            <div
                class="card space"
                style="
                    border-left:
                    5px solid
                    {% if item.validation == 'passed' %}
                        #147d3f
                    {% elif item.validation == 'mismatch' %}
                        #c62828
                    {% else %}
                        #d8a400
                    {% endif %};
                "
            >

                <div
                    style="
                        display:flex;
                        justify-content:
                            space-between;
                        gap:15px;
                        flex-wrap:wrap;
                        align-items:center;
                    "
                >

                    <div>
                        <h3
                            style="
                                margin:0 0 7px;
                            "
                        >
                            {{ item.name }}
                        </h3>

                        {% if item.document %}

                            <span
                                class="
                                    status green
                                "
                            >
                                версия
                                {{
                                    item.document.version
                                }}
                            </span>

                            {% if item.verified %}
                                <span
                                    class="
                                        status blue
                                    "
                                >
                                    Проверено
                                    аттестацией
                                </span>
                            {% endif %}

                            {% if
                                item.document
                                .replaced_by_attestation
                            %}
                                <span
                                    class="
                                        status yellow
                                    "
                                >
                                    Заменено
                                    аттестацией
                                </span>
                            {% endif %}

                            <div
                                class="
                                    muted space
                                "
                            >
                                Загрузил:
                                {{
                                    item.document
                                    .uploader_name
                                    or '—'
                                }}
                            </div>

                        {% else %}

                            <span
                                class="status gray"
                            >
                                Нет файла
                            </span>

                        {% endif %}
                    </div>

                    <div
                        style="
                            display:flex;
                            gap:8px;
                            flex-wrap:wrap;
                        "
                    >

                        {% if item.document %}

                            <a
                                class="
                                    btn
                                    btn-secondary
                                    btn-small
                                "
                                target="_blank"
                                href="{{ url_for(
                                    'download_document',
                                    document_id=
                                        item.document.id
                                ) }}"
                            >
                                Открыть документ
                            </a>

                        {% endif %}

                        {% if can_edit %}

                            <a
                                class="
                                    btn
                                    btn-primary
                                    btn-small
                                "
                                href="{{ url_for(
                                    'attestation_replace_document_v9',
                                    student_id=
                                        student.id,
                                    document_type=
                                        item.code
                                ) }}"
                            >
                                {% if item.document %}
                                    Заменить документ
                                {% else %}
                                    Загрузить документ
                                {% endif %}
                            </a>

                        {% endif %}

                    </div>
                </div>

                {% if item.document %}

                    {% if item.fields %}

                        <h4>
                            Распознанные
                            реквизиты
                        </h4>

                        <form
                            method="post"
                            action="{{ url_for(
                                'attestation_update_document_data_v9',
                                student_id=
                                    student.id,
                                document_id=
                                    item.document.id
                            ) }}"
                        >

                            <input
                                type="hidden"
                                name="csrf_token"
                                value="{{ csrf_token }}"
                            >

                            <div
                                style="
                                    overflow-x:auto;
                                "
                            >
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Поле</th>
                                            <th>
                                                Распознано OCR
                                            </th>
                                            <th>
                                                Сейчас
                                                в карточке
                                            </th>
                                            <th>
                                                Проверить /
                                                исправить
                                            </th>
                                        </tr>
                                    </thead>

                                    <tbody>

                                        {% for field
                                           in item.fields %}

                                            <tr>
                                                <td>
                                                    <strong>
                                                        {{
                                                            field.label
                                                        }}
                                                    </strong>
                                                </td>

                                                <td>
                                                    {{
                                                        field
                                                        .recognized
                                                    }}
                                                </td>

                                                <td>
                                                    {{
                                                        field
                                                        .current_display
                                                    }}
                                                </td>

                                                <td>

                                                    {% if
                                                        field.type
                                                        == 'date'
                                                    %}

                                                        <input
                                                            type="date"
                                                            name="
                                                                field_{{
                                                                    field.field
                                                                }}
                                                            "
                                                            value="{{
                                                                field.current
                                                            }}"
                                                            {% if
                                                                not can_edit
                                                            %}
                                                                disabled
                                                            {% endif %}
                                                        >

                                                    {% elif
                                                        field.type
                                                        == 'gender'
                                                    %}

                                                        <select
                                                            name="
                                                                field_{{
                                                                    field.field
                                                                }}
                                                            "
                                                            {% if
                                                                not can_edit
                                                            %}
                                                                disabled
                                                            {% endif %}
                                                        >
                                                            <option
                                                                value=""
                                                            >
                                                                —
                                                            </option>

                                                            <option
                                                                value="Мужской"
                                                                {% if
                                                                    field.current
                                                                    == 'Мужской'
                                                                %}
                                                                    selected
                                                                {% endif %}
                                                            >
                                                                Мужской
                                                            </option>

                                                            <option
                                                                value="Женский"
                                                                {% if
                                                                    field.current
                                                                    == 'Женский'
                                                                %}
                                                                    selected
                                                                {% endif %}
                                                            >
                                                                Женский
                                                            </option>
                                                        </select>

                                                    {% else %}

                                                        <input
                                                            name="
                                                                field_{{
                                                                    field.field
                                                                }}
                                                            "
                                                            value="{{
                                                                field.current
                                                            }}"
                                                            {% if
                                                                not can_edit
                                                            %}
                                                                disabled
                                                            {% endif %}
                                                        >

                                                    {% endif %}

                                                </td>
                                            </tr>

                                        {% endfor %}

                                    </tbody>
                                </table>
                            </div>

                            {% if can_edit %}

                                <button
                                    class="
                                        btn
                                        btn-primary
                                        space
                                    "
                                    type="submit"
                                >
                                    Сохранить данные
                                    и отметить
                                    проверенным
                                </button>

                            {% endif %}

                        </form>

                    {% endif %}

                    {% if item.checks %}

                        <details class="space">
                            <summary>
                                Результат
                                автоматической
                                проверки
                            </summary>

                            <div class="space">

                                {% for check
                                   in item.checks %}

                                    <div>
                                        {% if
                                            check.level
                                            == 'green'
                                        %}
                                            ✓
                                        {% elif
                                            check.level
                                            == 'red'
                                        %}
                                            ✕
                                        {% else %}
                                            !
                                        {% endif %}

                                        {{
                                            check.text
                                        }}
                                    </div>

                                {% endfor %}

                            </div>
                        </details>

                    {% endif %}

                    {% if item.ocr_text %}

                        <details class="space">
                            <summary>
                                Показать весь
                                распознанный
                                текст OCR
                            </summary>

                            <pre
                                style="
                                    white-space:
                                        pre-wrap;
                                    max-height:
                                        300px;
                                    overflow:auto;
                                    background:
                                        #f5f5f5;
                                    padding:12px;
                                "
                            >{{ item.ocr_text }}</pre>
                        </details>

                    {% endif %}

                {% endif %}

            </div>

        {% endfor %}
        """

        return render_template_string(
            template,
            student=student,
            items=items,
            can_edit=can_edit(
                student
            ),
csrf_token=session[
    "csrf_token"
],
        )

    # ========================================================
    # Оборачиваем существующую страницу review_student
    # ========================================================

    original_review = (
        app.view_functions[
            "review_student"
        ]
    )

    def review_student_v9(
        student_id: int,
    ):

        result = original_review(
            student_id
        )

        # POST обычно возвращает redirect.
        if (
            request.method != "GET"
            or not isinstance(
                result,
                str,
            )
        ):
            return result

        student = get_student_or_404(
            student_id
        )

        block = review_tools_html(
            student
        )

        marker = (
            "<h2>Добавить замечание</h2>"
        )

        if marker in result:

            return result.replace(
                marker,
                block
                + marker,
                1,
            )

        return result.replace(
            "</section>",
            block
            + "</section>",
            1,
        )

    app.view_functions[
        "review_student"
    ] = roles_required(
        "attestation",
        "admin",
    )(
        review_student_v9
    )

    # ========================================================
    # URL
    # ========================================================

    if (
        "attestation_update_document_data_v9"
        not in app.view_functions
    ):

        app.add_url_rule(
            (
                "/review/"
                "<int:student_id>/"
                "documents/"
                "<int:document_id>/data"
            ),
            endpoint=
                "attestation_update_document_data_v9",

            view_func=roles_required(
                "attestation",
                "admin",
            )(
                update_document_data
            ),

            methods=["POST"],
        )

    if (
        "attestation_replace_document_v9"
        not in app.view_functions
    ):

        app.add_url_rule(
            (
                "/review/"
                "<int:student_id>/"
                "documents/"
                "<document_type>/replace"
            ),
            endpoint=
                "attestation_replace_document_v9",

            view_func=roles_required(
                "attestation",
                "admin",
            )(
                replace_document
            ),

            methods=[
                "GET",
                "POST",
            ],
        )
