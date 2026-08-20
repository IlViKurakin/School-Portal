from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_PATH = ROOT / "app.py"
MODULE_PATH = ROOT / "portal_review_admin_v9_9.py"


MODULE_SOURCE = r'''
from __future__ import annotations

import html
import inspect
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from flask import abort, flash, g, redirect, request, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from werkzeug.security import generate_password_hash


MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_FILES = 30
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def apply_review_admin_v9_9(app, namespace):
    if app.config.get("_REVIEW_ADMIN_V99_APPLIED"):
        return

    app.config["_REVIEW_ADMIN_V99_APPLIED"] = True

    database_path = Path(namespace["DATABASE_PATH"])
    upload_dir = Path(namespace["UPLOAD_DIR"])

    get_db = namespace["get_db"]
    get_student_or_404 = namespace["get_student_or_404"]
    roles_required = namespace["roles_required"]
    audit = namespace["audit"]
    render_page = namespace["render_page"]
    full_name = namespace["full_name"]
    build_document_checklist = namespace["build_document_checklist"]
    missing_required_documents = namespace["missing_required_documents"]
    document_types = namespace["DOCUMENT_TYPES"]
    comment_categories = namespace["COMMENT_CATEGORIES"]

    original_review_student = app.view_functions.get("review_student")


    # =========================================================
    # База
    # =========================================================

    def migrate():
        connection = sqlite3.connect(database_path)

        try:
            connection.execute("PRAGMA foreign_keys = ON")

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_review_values (
                    document_id INTEGER NOT NULL,
                    field_key TEXT NOT NULL,
                    field_value TEXT,
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL,

                    PRIMARY KEY (document_id, field_key),

                    FOREIGN KEY (document_id)
                        REFERENCES documents(id),

                    FOREIGN KEY (updated_by)
                        REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS user_branch_access (
                    user_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,

                    PRIMARY KEY (user_id, branch_id),

                    FOREIGN KEY (user_id)
                        REFERENCES users(id),

                    FOREIGN KEY (branch_id)
                        REFERENCES branches(id)
                );
                """
            )

            connection.execute(
                """
                INSERT OR IGNORE INTO user_branch_access (
                    user_id,
                    branch_id
                )
                SELECT
                    id,
                    branch_id
                FROM users
                WHERE role = 'branch'
                  AND branch_id IS NOT NULL
                """
            )

            connection.commit()

        finally:
            connection.close()

    migrate()


    # =========================================================
    # Несколько филиалов для МУП
    # =========================================================

    @app.before_request
    def v99_apply_active_branch():
        current = getattr(g, "current_user", None)

        if not current:
            return

        if current["role"] != "branch":
            return

        rows = get_db().execute(
            """
            SELECT
                branches.id,
                branches.name
            FROM user_branch_access
            JOIN branches
                ON branches.id = user_branch_access.branch_id
            WHERE user_branch_access.user_id = ?
            ORDER BY branches.name
            """,
            (current["id"],),
        ).fetchall()

        if not rows:
            return

        allowed = {int(row["id"]): row["name"] for row in rows}

        try:
            active_branch_id = int(
                session.get("active_branch_id") or 0
            )
        except (TypeError, ValueError):
            active_branch_id = 0

        if active_branch_id not in allowed:
            primary = int(current["branch_id"] or 0)

            if primary in allowed:
                active_branch_id = primary
            else:
                active_branch_id = next(iter(allowed))

            session["active_branch_id"] = active_branch_id

        user = dict(current)
        user["branch_id"] = active_branch_id
        user["branch_name"] = allowed[active_branch_id]
        g.current_user = user


    def switch_branch_v99():
        if not g.current_user:
            abort(403)

        if g.current_user["role"] != "branch":
            abort(403)

        branch_id = request.form.get("branch_id", type=int)

        allowed = get_db().execute(
            """
            SELECT 1
            FROM user_branch_access
            WHERE user_id = ?
              AND branch_id = ?
            """,
            (
                g.current_user["id"],
                branch_id,
            ),
        ).fetchone()

        if not allowed:
            abort(403)

        session["active_branch_id"] = branch_id

        return redirect(
            request.referrer or url_for("students")
        )


    app.add_url_rule(
        "/switch-branch-v99",
        endpoint="switch_branch_v99",
        view_func=roles_required("branch")(
            switch_branch_v99
        ),
        methods=["POST"],
    )


    @app.after_request
    def v99_branch_switcher(response):
        current = getattr(g, "current_user", None)

        if (
            not current
            or current["role"] != "branch"
            or not response.content_type
            or "text/html" not in response.content_type
        ):
            return response

        rows = get_db().execute(
            """
            SELECT
                branches.id,
                branches.name
            FROM user_branch_access
            JOIN branches
                ON branches.id = user_branch_access.branch_id
            WHERE user_branch_access.user_id = ?
            ORDER BY branches.name
            """,
            (current["id"],),
        ).fetchall()

        if len(rows) <= 1:
            return response

        try:
            body = response.get_data(as_text=True)
        except Exception:
            return response

        if "</aside>" not in body:
            return response

        options = []

        active = int(current["branch_id"])

        for row in rows:
            selected = (
                " selected"
                if int(row["id"]) == active
                else ""
            )

            options.append(
                '<option value="{}"{}>{}</option>'.format(
                    int(row["id"]),
                    selected,
                    html.escape(str(row["name"])),
                )
            )

        switcher = """
        <div style="
            margin:18px 20px 0;
            padding:14px;
            border:1px solid #3b3b3b;
            border-radius:10px;
        ">
            <div style="
                font-size:13px;
                margin-bottom:7px;
                color:#ccc;
            ">
                Рабочий филиал
            </div>

            <form
                method="post"
                action="{action}"
            >
                <input
                    type="hidden"
                    name="csrf_token"
                    value="{csrf}"
                >

                <select
                    name="branch_id"
                    onchange="this.form.submit()"
                    style="width:100%"
                >
                    {options}
                </select>
            </form>
        </div>
        """.format(
            action=url_for("switch_branch_v99"),
            csrf=html.escape(
                str(session.get("csrf_token", ""))
            ),
            options="".join(options),
        )

        body = body.replace(
            "</aside>",
            switcher + "</aside>",
            1,
        )

        response.set_data(body)
        response.headers["Content-Length"] = str(
            len(response.get_data())
        )

        return response


    # =========================================================
    # Вспомогательные функции OCR
    # =========================================================

    def row_value(row, key, default=""):
        if not row:
            return default

        try:
            if key in row.keys():
                value = row[key]
                return default if value is None else value
        except Exception:
            pass

        return default


    def normalize_text(value):
        return re.sub(
            r"[^0-9a-zа-яё]+",
            "",
            str(value or "").lower(),
        )


    def get_review_values(document_id):
        rows = get_db().execute(
            """
            SELECT field_key, field_value
            FROM document_review_values
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchall()

        return {
            row["field_key"]: row["field_value"] or ""
            for row in rows
        }


    def load_ocr_json(document):
        raw = row_value(document, "ocr_json", "")

        if not raw:
            return {}

        try:
            return json.loads(raw)
        except Exception:
            return {}


    def collect_ocr_text(value):
        result = []

        def walk(obj):
            if len(result) > 300:
                return

            if isinstance(obj, dict):
                for key, child in obj.items():
                    low_key = str(key).lower()

                    if isinstance(child, str):
                        if (
                            "text" in low_key
                            or low_key in {
                                "rec_text",
                                "rec_texts",
                                "fulltext",
                                "full_text",
                            }
                        ):
                            if child.strip():
                                result.append(child.strip())

                    elif isinstance(child, list):
                        if (
                            "text" in low_key
                            and all(
                                isinstance(item, str)
                                for item in child
                            )
                        ):
                            result.extend(
                                item.strip()
                                for item in child
                                if item.strip()
                            )
                        else:
                            walk(child)

                    else:
                        walk(child)

            elif isinstance(obj, list):
                for child in obj:
                    walk(child)

        walk(value)

        return "\n".join(result)[:30000]


    def get_ocr_text(document):
        text = str(
            row_value(document, "ocr_text", "")
            or ""
        ).strip()

        if text:
            return text

        return collect_ocr_text(
            load_ocr_json(document)
        )


    def deep_lookup(value, wanted_keys):
        wanted = {
            normalize_text(item)
            for item in wanted_keys
        }

        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = normalize_text(key)

                if normalized_key in wanted:
                    if isinstance(
                        child,
                        (str, int, float),
                    ):
                        text = str(child).strip()

                        if text:
                            return text

                found = deep_lookup(
                    child,
                    wanted_keys,
                )

                if found:
                    return found

        elif isinstance(value, list):
            for child in value:
                found = deep_lookup(
                    child,
                    wanted_keys,
                )

                if found:
                    return found

        return ""


    def person_match(text, last_name, first_name):
        if not text.strip():
            return None

        normalized = normalize_text(text)
        last = normalize_text(last_name)
        first = normalize_text(first_name)

        if not last or not first:
            return None

        return (
            last in normalized
            and first in normalized
        )


    TYPE_KEYWORDS = {
        "attachment_application": (
            "заявлен",
            "прикреп",
        ),
        "withdrawal_application": (
            "заявлен",
            "отчисл",
        ),
        "birth_certificate": (
            "свидетельств",
            "рожд",
        ),
        "child_registration": (
            "регистрац",
            "зарегистр",
        ),
        "child_passport": (
            "паспорт",
        ),
        "parent_passport": (
            "паспорт",
        ),
        "child_snils": (
            "страх",
            "снилс",
        ),
        "parent_snils": (
            "страх",
            "снилс",
        ),
        "personal_file": (
            "личн",
            "аттестац",
            "справк",
        ),
        "parent_consent": (
            "соглас",
        ),
        "child_consent": (
            "соглас",
        ),
        "education_notice": (
            "уведом",
        ),
        "grade9_certificate": (
            "аттестат",
        ),
        "oge_results": (
            "огэ",
            "экзамен",
        ),
        "relation_proof": (
            "опек",
            "усынов",
            "смен",
            "брак",
        ),
    }


    def type_match(document_type, text):
        if not text.strip():
            return None

        keywords = TYPE_KEYWORDS.get(document_type)

        if not keywords:
            return None

        normalized = normalize_text(text)

        return any(
            normalize_text(keyword) in normalized
            for keyword in keywords
        )


    def automatic_match_status(
        document_type,
        text,
        student,
    ):
        target_parent = document_type in {
            "attachment_application",
            "withdrawal_application",
            "parent_passport",
            "parent_snils",
            "parent_consent",
            "relation_proof",
        }

        if target_parent:
            fio_result = person_match(
                text,
                student["parent_last_name"],
                student["parent_first_name"],
            )
        else:
            fio_result = person_match(
                text,
                student["last_name"],
                student["first_name"],
            )

        type_result = type_match(
            document_type,
            text,
        )

        if fio_result is False:
            return "Не совпадает"

        if type_result is False:
            return "Не совпадает"

        if fio_result is None:
            return "Не распознано"

        if type_result is None:
            return "Не распознано"

        return "Совпадает"


    def format_snils(value):
        digits = re.sub(r"\D", "", str(value or ""))

        if len(digits) != 11:
            return str(value or "").strip()

        return (
            f"{digits[:3]}-"
            f"{digits[3:6]}-"
            f"{digits[6:9]} "
            f"{digits[9:]}"
        )


    def snils_from_text(text):
        match = re.search(
            r"(?<!\d)"
            r"(\d{3})[\s-]*"
            r"(\d{3})[\s-]*"
            r"(\d{3})[\s-]*"
            r"(\d{2})"
            r"(?!\d)",
            text,
        )

        if not match:
            return ""

        return format_snils(
            "".join(match.groups())
        )


    def birth_certificate_from_text(text):
        match = re.search(
            r"\b"
            r"([IVXLCDM]+[\s\-–—]*[А-ЯЁ]{2})"
            r"\s*(?:№|N)?\s*"
            r"(\d{6})"
            r"\b",
            text.upper(),
        )

        if not match:
            return "", ""

        series = (
            match.group(1)
            .replace(" ", "")
            .replace("–", "-")
            .replace("—", "-")
        )

        return series, match.group(2)


    def registration_address_from_text(text):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line.strip()
        ]

        for index, line in enumerate(lines):
            low = line.lower()

            if (
                "зарегистр" in low
                or "месту жительства" in low
                or "месту пребывания" in low
            ):
                result = " ".join(
                    lines[index:index + 5]
                ).strip()

                if len(result) > 15:
                    return result[:500]

        address_lines = [
            line
            for line in lines
            if any(
                token in line.lower()
                for token in (
                    "ул.",
                    "улиц",
                    "дом",
                    "д.",
                    "кв.",
                    "квартир",
                    "город",
                    "гор.",
                )
            )
        ]

        if address_lines:
            return " ".join(address_lines[:5])[:500]

        return ""


    def get_display_data(
        document_type,
        document,
        student,
    ):
        if not document:
            return [], []

        document_id = document["id"]
        corrected = get_review_values(document_id)
        ocr_json = load_ocr_json(document)
        text = get_ocr_text(document)

        automatic_status = automatic_match_status(
            document_type,
            text,
            student,
        )

        match_status = corrected.get(
            "match_status",
            automatic_status,
        )

        display = []
        editable = []

        def json_value(keys):
            return deep_lookup(
                ocr_json,
                keys,
            )

        if document_type == "birth_certificate":
            raw_series, raw_number = (
                birth_certificate_from_text(text)
            )

            series = (
                corrected.get("certificate_series")
                or json_value(
                    (
                        "birth_certificate_series",
                        "certificate_series",
                    )
                )
                or raw_series
                or student["birth_certificate_series"]
                or ""
            )

            number = (
                corrected.get("certificate_number")
                or json_value(
                    (
                        "birth_certificate_number",
                        "certificate_number",
                    )
                )
                or raw_number
                or student["birth_certificate_number"]
                or ""
            )

            display.extend(
                [
                    ("Серия", series or "Не распознано"),
                    ("Номер", number or "Не распознано"),
                    ("Проверка ФИО/типа", match_status),
                ]
            )

            editable.extend(
                [
                    {
                        "key": "certificate_series",
                        "label": "Серия",
                        "value": series,
                        "kind": "text",
                    },
                    {
                        "key": "certificate_number",
                        "label": "Номер",
                        "value": number,
                        "kind": "text",
                    },
                    {
                        "key": "match_status",
                        "label": "Результат проверки",
                        "value": match_status,
                        "kind": "status",
                    },
                ]
            )

        elif document_type == "child_registration":
            address = (
                corrected.get("registration_address")
                or json_value(
                    (
                        "registration_address",
                        "registered_address",
                        "address_registration",
                        "address",
                    )
                )
                or registration_address_from_text(text)
            )

            display.append(
                ("Проверка ФИО/типа", match_status)
            )

            if match_status == "Совпадает":
                display.append(
                    (
                        "Адрес регистрации",
                        address or "Адрес не распознан",
                    )
                )
            elif match_status == "Не совпадает":
                display.append(
                    (
                        "Адрес регистрации",
                        "ФИО ребенка не совпадает",
                    )
                )
            else:
                display.append(
                    (
                        "Адрес регистрации",
                        "Невозможно распознать ФИО ребенка",
                    )
                )

            editable.extend(
                [
                    {
                        "key": "registration_address",
                        "label": "Адрес регистрации",
                        "value": address,
                        "kind": "text",
                    },
                    {
                        "key": "match_status",
                        "label": "Результат проверки",
                        "value": match_status,
                        "kind": "status",
                    },
                ]
            )

        elif document_type in {
            "child_passport",
            "parent_passport",
        }:
            if document_type == "child_passport":
                card_series = (
                    student["child_passport_series"]
                    or ""
                )
                card_number = (
                    student["child_passport_number"]
                    or ""
                )
            else:
                card_series = (
                    student["parent_passport_series"]
                    or ""
                )
                card_number = (
                    student["parent_passport_number"]
                    or ""
                )

            series = (
                corrected.get("passport_series")
                or json_value(
                    (
                        "passport_series",
                        "series",
                    )
                )
                or card_series
            )

            number = (
                corrected.get("passport_number")
                or json_value(
                    (
                        "passport_number",
                        "number",
                    )
                )
                or card_number
            )

            display.extend(
                [
                    ("Серия", series or "Не распознано"),
                    ("Номер", number or "Не распознано"),
                    ("Проверка ФИО/типа", match_status),
                ]
            )

            editable.extend(
                [
                    {
                        "key": "passport_series",
                        "label": "Серия",
                        "value": series,
                        "kind": "text",
                    },
                    {
                        "key": "passport_number",
                        "label": "Номер",
                        "value": number,
                        "kind": "text",
                    },
                    {
                        "key": "match_status",
                        "label": "Результат проверки",
                        "value": match_status,
                        "kind": "status",
                    },
                ]
            )

        elif document_type in {
            "child_snils",
            "parent_snils",
        }:
            card_value = (
                student["student_snils"]
                if document_type == "child_snils"
                else student["parent_snils"]
            ) or ""

            snils = (
                corrected.get("snils")
                or json_value(
                    (
                        "student_snils",
                        "parent_snils",
                        "snils",
                    )
                )
                or snils_from_text(text)
                or card_value
            )

            display.extend(
                [
                    (
                        "Номер СНИЛС",
                        snils or "Не распознано",
                    ),
                    (
                        "Проверка ФИО/типа",
                        match_status,
                    ),
                ]
            )

            editable.extend(
                [
                    {
                        "key": "snils",
                        "label": "Номер СНИЛС",
                        "value": snils,
                        "kind": "text",
                    },
                    {
                        "key": "match_status",
                        "label": "Результат проверки",
                        "value": match_status,
                        "kind": "status",
                    },
                ]
            )

        elif document_type == "citizenship_mark":
            display.append(
                (
                    "Проверка",
                    "Ручная проверка документа",
                )
            )

        else:
            status = corrected.get(
                "match_status",
                match_status,
            )

            display.append(
                (
                    "Проверка типа и ФИО",
                    status,
                )
            )

            editable.append(
                {
                    "key": "match_status",
                    "label": "Результат проверки",
                    "value": status,
                    "kind": "status",
                }
            )

        return display, editable


    def can_edit_review(student):
        if g.current_user["role"] == "admin":
            return True

        return (
            student["status"] == "in_review"
            and int(student["assigned_to"] or 0)
            == int(g.current_user["id"])
        )


    # =========================================================
    # Безопасное сохранение ручной коррекции OCR
    # =========================================================

    CARD_FIELDS = {
        ("birth_certificate", "certificate_series"):
            "birth_certificate_series",

        ("birth_certificate", "certificate_number"):
            "birth_certificate_number",

        ("child_passport", "passport_series"):
            "child_passport_series",

        ("child_passport", "passport_number"):
            "child_passport_number",

        ("parent_passport", "passport_series"):
            "parent_passport_series",

        ("parent_passport", "passport_number"):
            "parent_passport_number",

        ("child_snils", "snils"):
            "student_snils",

        ("parent_snils", "snils"):
            "parent_snils",
    }


    ALLOWED_EDIT_FIELDS = {
        "birth_certificate": {
            "certificate_series",
            "certificate_number",
            "match_status",
        },
        "child_registration": {
            "registration_address",
            "match_status",
        },
        "child_passport": {
            "passport_series",
            "passport_number",
            "match_status",
        },
        "parent_passport": {
            "passport_series",
            "passport_number",
            "match_status",
        },
        "child_snils": {
            "snils",
            "match_status",
        },
        "parent_snils": {
            "snils",
            "match_status",
        },
        "attachment_application": {
            "match_status",
        },
        "withdrawal_application": {
            "match_status",
        },
        "personal_file": {
            "match_status",
        },
        "parent_consent": {
            "match_status",
        },
        "child_consent": {
            "match_status",
        },
        "education_notice": {
            "match_status",
        },
        "grade9_certificate": {
            "match_status",
        },
        "oge_results": {
            "match_status",
        },
        "relation_proof": {
            "match_status",
        },
    }


    def save_ocr_correction(student):
        if not can_edit_review(student):
            abort(403)

        document_id = request.form.get(
            "document_id",
            type=int,
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
                student["id"],
            ),
        ).fetchone()

        if not document:
            abort(404)

        document_type = document["document_type"]

        allowed = ALLOWED_EDIT_FIELDS.get(
            document_type,
            set(),
        )

        if not allowed:
            abort(400)

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        card_updates = {}
        saved = []

        for field_key in allowed:
            if field_key not in request.form:
                continue

            value = request.form.get(
                field_key,
                "",
            ).strip()

            # ВАЖНО:
            # пустое поле не стирает существующее значение.
            if not value:
                continue

            if field_key == "snils":
                digits = re.sub(r"\D", "", value)

                if len(digits) != 11:
                    flash(
                        "СНИЛС должен содержать 11 цифр.",
                        "error",
                    )
                    return False

                value = format_snils(digits)

            if field_key == "passport_series":
                digits = re.sub(r"\D", "", value)

                if len(digits) != 4:
                    flash(
                        "Серия паспорта должна содержать 4 цифры.",
                        "error",
                    )
                    return False

                value = digits

            if field_key == "passport_number":
                digits = re.sub(r"\D", "", value)

                if len(digits) != 6:
                    flash(
                        "Номер паспорта должен содержать 6 цифр.",
                        "error",
                    )
                    return False

                value = digits

            if field_key == "match_status":
                if value not in {
                    "Совпадает",
                    "Не совпадает",
                    "Не распознано",
                }:
                    flash(
                        "Некорректный результат проверки.",
                        "error",
                    )
                    return False

            get_db().execute(
                """
                INSERT INTO document_review_values (
                    document_id,
                    field_key,
                    field_value,
                    updated_by,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)

                ON CONFLICT(document_id, field_key)
                DO UPDATE SET
                    field_value = excluded.field_value,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    field_key,
                    value,
                    g.current_user["id"],
                    now,
                ),
            )

            card_field = CARD_FIELDS.get(
                (
                    document_type,
                    field_key,
                )
            )

            if card_field:
                card_updates[card_field] = value

            saved.append(
                f"{field_key}={value}"
            )

        # Обновляются ТОЛЬКО конкретно исправленные поля.
        if card_updates:
            assignments = ", ".join(
                f"{column} = ?"
                for column in card_updates
            )

            get_db().execute(
                f"""
                UPDATE students
                SET
                    {assignments},
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    *card_updates.values(),
                    now,
                    student["id"],
                ),
            )

        get_db().commit()

        audit(
            "ocr_data_corrected",
            student["id"],
            (
                f"document_id={document_id}; "
                + "; ".join(saved)
            ),
        )

        flash(
            "Исправления распознанных данных сохранены.",
            "success",
        )

        return True


    # =========================================================
    # Поиск действующего OCR-процессора предыдущих патчей
    # =========================================================

    def find_existing_ocr_processor():
        start = app.view_functions.get(
            "upload_document"
        )

        if not start:
            return None

        seen = set()
        stack = [start]

        preferred_names = {
            "process_document_ocr",
            "run_document_ocr",
            "process_ocr",
            "run_ocr_for_document",
        }

        while stack:
            obj = stack.pop()

            if not callable(obj):
                continue

            object_id = id(obj)

            if object_id in seen:
                continue

            seen.add(object_id)

            name = getattr(
                obj,
                "__name__",
                "",
            )

            try:
                signature = inspect.signature(obj)

                required = [
                    p
                    for p in signature.parameters.values()
                    if (
                        p.default
                        is inspect.Parameter.empty
                        and p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    )
                ]
            except Exception:
                required = []

            if (
                name in preferred_names
                and len(required) <= 1
            ):
                return obj

            wrapped = getattr(
                obj,
                "__wrapped__",
                None,
            )

            if callable(wrapped):
                stack.append(wrapped)

            closure = getattr(
                obj,
                "__closure__",
                None,
            )

            if closure:
                for cell in closure:
                    try:
                        child = cell.cell_contents
                    except Exception:
                        continue

                    if callable(child):
                        child_name = getattr(
                            child,
                            "__name__",
                            "",
                        )

                        if (
                            child_name in preferred_names
                            or (
                                "ocr" in child_name.lower()
                                and "document"
                                in child_name.lower()
                            )
                        ):
                            stack.append(child)

            globals_dict = getattr(
                obj,
                "__globals__",
                {},
            )

            for candidate_name in preferred_names:
                candidate = globals_dict.get(
                    candidate_name
                )

                if callable(candidate):
                    stack.append(candidate)

        return None


    # =========================================================
    # Замена документа сотрудником аттестации
    # =========================================================

    def validate_upload(upload):
        name = Path(
            str(upload.filename or "")
            .replace("\\", "/")
        ).name.strip()

        if not name:
            raise ValueError(
                "Не выбрано имя файла."
            )

        suffix = Path(name).suffix.lower()

        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Файл «{name}»: разрешены PDF, JPG, JPEG, PNG."
            )

        data = upload.read()

        if not data:
            raise ValueError(
                f"Файл «{name}» пустой."
            )

        if len(data) > MAX_FILE_SIZE:
            raise ValueError(
                f"Файл «{name}» больше 25 МБ."
            )

        return name, suffix, data


    def image_reader(data):
        try:
            image = Image.open(io.BytesIO(data))
            image = ImageOps.exif_transpose(image)
            image.load()

        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
        ) as error:
            raise ValueError(
                f"Не удалось прочитать изображение: {error}"
            ) from error

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
        if not uploads:
            raise ValueError(
                "Выберите хотя бы один файл."
            )

        if len(uploads) > MAX_FILES:
            raise ValueError(
                "Можно выбрать не более 30 файлов."
            )

        writer = PdfWriter()
        names = []
        pages = 0

        for upload in uploads:
            name, suffix, data = validate_upload(
                upload
            )

            names.append(name)

            if suffix == ".pdf":
                pdf_position = data[:1024].find(
                    b"%PDF"
                )

                if pdf_position < 0:
                    raise ValueError(
                        f"Файл «{name}» не является корректным PDF."
                    )

                reader = PdfReader(
                    io.BytesIO(
                        data[pdf_position:]
                    )
                )

                if reader.is_encrypted:
                    raise ValueError(
                        f"Файл «{name}» защищён паролем."
                    )

            else:
                reader = image_reader(data)

            for page in reader.pages:
                writer.add_page(page)
                pages += 1

        if not pages:
            raise ValueError(
                "В выбранных файлах нет страниц."
            )

        output = io.BytesIO()
        writer.write(output)

        return (
            output.getvalue(),
            names,
            pages,
        )


    def replace_document_v99(
        student_id,
        document_type,
    ):
        student = get_student_or_404(
            student_id
        )

        if not can_edit_review(student):
            abort(403)

        if document_type not in document_types:
            abort(404)

        if request.method == "GET":
            body = """
            <h1>Заменить документ</h1>

            <div class="card">
                <p>
                    <strong>Ученик:</strong>
                    {{ student.last_name }}
                    {{ student.first_name }}
                    {{ student.middle_name or '' }}
                </p>

                <p>
                    <strong>Документ:</strong>
                    {{ document_name }}
                </p>

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
                        Новый файл или несколько страниц
                    </label>

                    <input
                        type="file"
                        name="files"
                        accept=".pdf,.jpg,.jpeg,.png"
                        multiple
                        required
                    >

                    <p class="muted">
                        Можно выбрать несколько PDF/JPG/PNG.
                        Они будут объединены в один документ.
                    </p>

                    <button
                        class="btn btn-primary"
                        type="submit"
                    >
                        Заменить документ
                    </button>

                    <a
                        class="btn btn-secondary"
                        href="{{ url_for(
                            'review_student',
                            student_id=student.id
                        ) }}"
                    >
                        Отмена
                    </a>
                </form>
            </div>
            """

            return render_page(
                "Замена документа",
                body,
                student=student,
                document_name=document_types[
                    document_type
                ]["name"],
            )

        uploads = [
            upload
            for upload
            in request.files.getlist("files")
            if upload and upload.filename
        ]

        if not uploads:
            uploads = [
                upload
                for upload
                in request.files.getlist("file")
                if upload and upload.filename
            ]

        try:
            merged, names, page_count = (
                merge_files(uploads)
            )
        except ValueError as error:
            flash(str(error), "error")

            return redirect(
                url_for(
                    "replace_document_v99",
                    student_id=student_id,
                    document_type=document_type,
                )
            )

        previous = get_db().execute(
            """
            SELECT *
            FROM documents
            WHERE student_id = ?
              AND document_type = ?
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (
                student_id,
                document_type,
            ),
        ).fetchone()

        version = (
            int(previous["version"]) + 1
            if previous
            else 1
        )

        source_document_id = (
            previous["id"]
            if previous
            else None
        )

        stored_name = (
            f"{uuid.uuid4().hex}.pdf"
        )

        upload_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        (upload_dir / stored_name).write_bytes(
            merged
        )

        document_columns = {
            row["name"]
            for row in get_db().execute(
                "PRAGMA table_info(documents)"
            ).fetchall()
        }

        insert_columns = [
            "student_id",
            "document_type",
            "original_name",
            "stored_name",
            "mime_type",
            "version",
            "source_document_id",
            "uploaded_by",
            "uploaded_at",
            "ocr_status",
        ]

        insert_values = [
            student_id,
            document_type,
            (
                names[0]
                if len(names) == 1
                else f"Объединено {len(names)} файлов"
            ),
            stored_name,
            "application/pdf",
            version,
            source_document_id,
            g.current_user["id"],
            datetime.now().isoformat(
                timespec="seconds"
            ),
            "not_configured",
        ]

        if "page_count" in document_columns:
            insert_columns.append(
                "page_count"
            )
            insert_values.append(
                page_count
            )

        placeholders = ", ".join(
            "?"
            for _ in insert_columns
        )

        cursor = get_db().execute(
            f"""
            INSERT INTO documents (
                {", ".join(insert_columns)}
            )
            VALUES ({placeholders})
            """,
            insert_values,
        )

        document_id = cursor.lastrowid
        get_db().commit()

        ocr_processor = (
            find_existing_ocr_processor()
        )

        if ocr_processor:
            try:
                ocr_processor(document_id)

            except Exception as error:
                get_db().execute(
                    """
                    UPDATE documents
                    SET ocr_status = 'error'
                    WHERE id = ?
                    """,
                    (document_id,),
                )
                get_db().commit()

                flash(
                    "Документ заменён, но OCR завершился "
                    f"с ошибкой: {error}",
                    "error",
                )
        else:
            flash(
                "Документ заменён. Для этого типа "
                "не найден действующий OCR-обработчик.",
                "error",
            )

        audit(
            "document_replaced_by_attestation",
            student_id,
            (
                f"document_type={document_type}; "
                f"document_id={document_id}; "
                f"version={version}; "
                f"pages={page_count}"
            ),
        )

        flash(
            "Документ заменён.",
            "success",
        )

        return redirect(
            url_for(
                "review_student",
                student_id=student_id,
            )
        )


    app.add_url_rule(
        "/review/<int:student_id>/replace/"
        "<document_type>",
        endpoint="replace_document_v99",
        view_func=roles_required(
            "attestation",
            "admin",
        )(
            replace_document_v99
        ),
        methods=["GET", "POST"],
    )


    # =========================================================
    # Новый единый экран проверки
    # =========================================================

    def review_student_v99(student_id):
        student = get_student_or_404(
            student_id
        )

        if request.method == "POST":
            action = request.form.get(
                "action",
                ""
            )

            if action == "save_ocr":
                if save_ocr_correction(
                    student
                ):
                    return redirect(
                        url_for(
                            "review_student",
                            student_id=student_id,
                        )
                    )

            if action == "ready":
                if not can_edit_review(student):
                    abort(403)

                missing = (
                    missing_required_documents(
                        student
                    )
                )

                open_comments = get_db().execute(
                    """
                    SELECT COUNT(*)
                    FROM comments
                    WHERE student_id = ?
                      AND is_open = 1
                    """,
                    (student_id,),
                ).fetchone()[0]

                if missing:
                    flash(
                        "Не загружены обязательные документы: "
                        + ", ".join(missing),
                        "error",
                    )

                elif open_comments:
                    flash(
                        "Есть открытые замечания.",
                        "error",
                    )

                else:
                    get_db().execute(
                        """
                        UPDATE students
                        SET
                            status = 'ready',
                            updated_at = ?
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
                        "student_ready",
                        student_id,
                    )

                    flash(
                        "Карточка готова к зачислению.",
                        "success",
                    )

                return redirect(
                    url_for(
                        "review_student",
                        student_id=student_id,
                    )
                )

            # Остальные рабочие действия:
            # взять в работу, замечание, возврат,
            # зачисление — оставляем из действующей версии.
            if original_review_student:
                return original_review_student(
                    student_id
                )

        # -------------------------
        # GET
        # -------------------------

        student = get_student_or_404(
            student_id
        )

        checklist = build_document_checklist(
            student
        )

        comments = get_db().execute(
            """
            SELECT
                comments.*,
                users.full_name AS author_name
            FROM comments
            JOIN users
                ON users.id = comments.created_by
            WHERE comments.student_id = ?
            ORDER BY comments.created_at DESC
            """,
            (student_id,),
        ).fetchall()

        editing_document_id = request.args.get(
            "edit_document",
            type=int,
        )

        rows = []

        for item in checklist:
            document = item.get(
                "document"
            )

            display = []
            editable = []

            if document:
                display, editable = (
                    get_display_data(
                        item["code"],
                        document,
                        student,
                    )
                )

            rows.append(
                {
                    "code": item["code"],
                    "name": item["name"],
                    "required": item["required"],
                    "document": document,
                    "display": display,
                    "editable": editable,
                }
            )

        editable_review = can_edit_review(
            student
        )

        body = """
        <div style="
            display:flex;
            justify-content:space-between;
            align-items:center;
            gap:20px;
        ">
            <div>
                <h1>
                    Проверка:
                    {{ student.last_name }}
                    {{ student.first_name }}
                </h1>

                <p class="muted">
                    {{ student.branch_name }}
                    · {{ student.class_number }} класс
                </p>
            </div>

            <span class="
                status
                {{ status_classes[student.status] }}
            ">
                {{ status_labels[student.status] }}
            </span>
        </div>

        {% if student.status == 'submitted' %}
            <form method="post">
                <input
                    type="hidden"
                    name="csrf_token"
                    value="{{ csrf_token }}"
                >
                <input
                    type="hidden"
                    name="action"
                    value="take"
                >

                <button class="btn btn-primary">
                    Взять в работу
                </button>
            </form>
        {% endif %}

        <div class="form-grid space">
            <div class="card">
                <h2>Ученик</h2>

                <p>
                    <strong>ФИО:</strong>
                    {{ student_full_name }}
                </p>

                <p>
                    <strong>Дата рождения:</strong>
                    {{ student.birth_date }}
                </p>

                <p>
                    <strong>СНИЛС:</strong>
                    {{ student.student_snils or '—' }}
                </p>

                <p>
                    <strong>Свидетельство:</strong>
                    {{ student.birth_certificate_series or '' }}
                    {{ student.birth_certificate_number or '—' }}
                </p>

                <p>
                    <strong>Паспорт:</strong>
                    {{ student.child_passport_series or '' }}
                    {{ student.child_passport_number or '—' }}
                </p>
            </div>

            <div class="card">
                <h2>Законный представитель</h2>

                <p>
                    <strong>ФИО:</strong>
                    {{ parent_full_name }}
                </p>

                <p>
                    <strong>Связь:</strong>
                    {{ student.relation_type }}
                </p>

                <p>
                    <strong>СНИЛС:</strong>
                    {{ student.parent_snils or '—' }}
                </p>

                <p>
                    <strong>Паспорт:</strong>
                    {{ student.parent_passport_series or '' }}
                    {{ student.parent_passport_number or '—' }}
                </p>
            </div>
        </div>


        <h2>Документы</h2>

        <div class="card">
            {% for item in rows %}
                <div style="
                    display:grid;
                    grid-template-columns:
                        minmax(260px, 1.5fr)
                        100px
                        minmax(280px, 1.4fr)
                        170px;
                    gap:20px;
                    align-items:start;
                    padding:22px 0;
                    border-bottom:1px solid #e8e8e8;
                ">
                    <div>
                        <strong>
                            {{ item.name }}
                            {% if item.required %}
                                <span class="required">*</span>
                            {% endif %}
                        </strong>
                    </div>

                    <div>
                        {% if item.document %}
                            <span class="status green">
                                Есть
                            </span>
                        {% else %}
                            <span class="status red">
                                Нет
                            </span>
                        {% endif %}
                    </div>

                    <div>
                        {% if item.document %}
                            {% for label, value in item.display %}
                                <div style="margin-bottom:7px">
                                    <strong>{{ label }}:</strong>
                                    {{ value }}
                                </div>
                            {% endfor %}

                            {% if
                                editable_review
                                and item.editable
                                and editing_document_id
                                    == item.document.id
                            %}
                                <form
                                    method="post"
                                    style="
                                        margin-top:15px;
                                        padding:15px;
                                        background:#f7f7f7;
                                        border-radius:10px;
                                    "
                                >
                                    <input
                                        type="hidden"
                                        name="csrf_token"
                                        value="{{ csrf_token }}"
                                    >

                                    <input
                                        type="hidden"
                                        name="action"
                                        value="save_ocr"
                                    >

                                    <input
                                        type="hidden"
                                        name="document_id"
                                        value="{{ item.document.id }}"
                                    >

                                    {% for field in item.editable %}
                                        <div style="margin-bottom:12px">
                                            <label>
                                                {{ field.label }}
                                            </label>

                                            {% if field.kind == 'status' %}
                                                <select
                                                    name="{{ field.key }}"
                                                >
                                                    {% for status in [
                                                        'Совпадает',
                                                        'Не совпадает',
                                                        'Не распознано'
                                                    ] %}
                                                        <option
                                                            value="{{ status }}"
                                                            {% if field.value == status %}
                                                                selected
                                                            {% endif %}
                                                        >
                                                            {{ status }}
                                                        </option>
                                                    {% endfor %}
                                                </select>
                                            {% else %}
                                                <input
                                                    name="{{ field.key }}"
                                                    value="{{ field.value }}"
                                                >
                                            {% endif %}
                                        </div>
                                    {% endfor %}

                                    <button
                                        class="btn btn-primary btn-small"
                                        type="submit"
                                    >
                                        Сохранить
                                    </button>

                                    <a
                                        class="btn btn-secondary btn-small"
                                        href="{{ url_for(
                                            'review_student',
                                            student_id=student.id
                                        ) }}"
                                    >
                                        Отмена
                                    </a>
                                </form>
                            {% endif %}
                        {% else %}
                            <span class="muted">
                                Документ не загружен
                            </span>
                        {% endif %}
                    </div>

                    <div>
                        {% if item.document %}
                            <div style="margin-bottom:8px">
                                <a
                                    class="btn btn-secondary btn-small"
                                    target="_blank"
                                    href="{{ url_for(
                                        'download_document',
                                        document_id=item.document.id
                                    ) }}"
                                >
                                    Открыть
                                </a>
                            </div>

                            {% if editable_review %}
                                <div style="margin-bottom:8px">
                                    <a
                                        class="btn btn-secondary btn-small"
                                        href="{{ url_for(
                                            'replace_document_v99',
                                            student_id=student.id,
                                            document_type=item.code
                                        ) }}"
                                    >
                                        Заменить
                                    </a>
                                </div>

                                {% if item.editable %}
                                    <div>
                                        <a
                                            class="btn btn-primary btn-small"
                                            href="{{ url_for(
                                                'review_student',
                                                student_id=student.id,
                                                edit_document=item.document.id
                                            ) }}"
                                        >
                                            Исправить
                                        </a>
                                    </div>
                                {% endif %}
                            {% endif %}
                        {% endif %}
                    </div>
                </div>
            {% endfor %}
        </div>


        <h2 class="space">Замечания</h2>

        {% if comments %}
            <div class="card">
                {% for comment in comments %}
                    <div style="
                        padding:12px 0;
                        border-bottom:1px solid #eee;
                    ">
                        <strong>
                            {{ comment.category }}
                        </strong>

                        {% if comment.document_type %}
                            ·
                            {{
                                document_types[
                                    comment.document_type
                                ].name
                            }}
                        {% endif %}

                        {% if comment.is_open %}
                            <span class="status red">
                                Открыто
                            </span>
                        {% else %}
                            <span class="status green">
                                Исправлено
                            </span>
                        {% endif %}

                        <p>{{ comment.text }}</p>

                        <div class="muted">
                            {{ comment.author_name }}
                            ·
                            {{
                                comment.created_at[:16]
                                .replace('T', ' ')
                            }}
                        </div>
                    </div>
                {% endfor %}
            </div>
        {% endif %}


        {% if editable_review %}
            <div class="card space">
                <h3>Добавить замечание</h3>

                <form method="post">
                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{{ csrf_token }}"
                    >

                    <input
                        type="hidden"
                        name="action"
                        value="comment"
                    >

                    <label>Документ</label>

                    <select name="document_type">
                        <option value="">
                            Общее замечание
                        </option>

                        {% for item in rows %}
                            <option value="{{ item.code }}">
                                {{ item.name }}
                            </option>
                        {% endfor %}
                    </select>

                    <label class="space">
                        Категория
                    </label>

                    <select name="category" required>
                        {% for category in categories %}
                            <option value="{{ category }}">
                                {{ category }}
                            </option>
                        {% endfor %}
                    </select>

                    <label class="space">
                        Комментарий
                    </label>

                    <textarea
                        name="text"
                        required
                    ></textarea>

                    <button
                        class="btn btn-primary space"
                        type="submit"
                    >
                        Добавить замечание
                    </button>
                </form>
            </div>

            <div class="space">
                <form
                    class="inline"
                    method="post"
                >
                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{{ csrf_token }}"
                    >
                    <input
                        type="hidden"
                        name="action"
                        value="return"
                    >

                    <button class="btn btn-secondary">
                        Вернуть на исправление
                    </button>
                </form>

                <form
                    class="inline"
                    method="post"
                >
                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{{ csrf_token }}"
                    >
                    <input
                        type="hidden"
                        name="action"
                        value="ready"
                    >

                    <button class="btn btn-green">
                        Готово к зачислению
                    </button>
                </form>
            </div>
        {% endif %}


        {% if student.status == 'ready' %}
            <div class="space">
                <form
                    class="inline"
                    method="post"
                >
                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{{ csrf_token }}"
                    >
                    <input
                        type="hidden"
                        name="action"
                        value="enroll"
                    >

                    <button class="btn btn-green">
                        Зачислен
                    </button>
                </form>
            </div>
        {% endif %}
        """

        return render_page(
            "Проверка карточки",
            body,
            student=student,
            rows=rows,
            comments=comments,
            document_types=document_types,
            categories=comment_categories,
            editing_document_id=editing_document_id,
            editable_review=editable_review,
            student_full_name=full_name(
                student["last_name"],
                student["first_name"],
                student["middle_name"],
            ),
            parent_full_name=full_name(
                student["parent_last_name"],
                student["parent_first_name"],
                student["parent_middle_name"],
            ),
        )


    app.view_functions["review_student"] = (
        roles_required(
            "attestation",
            "admin",
        )(
            review_student_v99
        )
    )


    # =========================================================
    # Администратор — редактирование пользователей
    # =========================================================

    def admin_users_v99():
        if request.method == "POST":
            action = request.form.get(
                "action",
                "create",
            )

            if action == "create":
                email = request.form.get(
                    "email",
                    "",
                ).strip().lower()

                full_user_name = request.form.get(
                    "full_name",
                    "",
                ).strip()

                role = request.form.get(
                    "role",
                    "",
                )

                password = request.form.get(
                    "password",
                    "",
                )

                branch_ids = []

                for value in request.form.getlist(
                    "branch_ids"
                ):
                    try:
                        branch_id = int(value)
                    except (TypeError, ValueError):
                        continue

                    if branch_id not in branch_ids:
                        branch_ids.append(branch_id)

                if not email.endswith(
                    "@top-academy.ru"
                ):
                    flash(
                        "Разрешены только адреса "
                        "@top-academy.ru.",
                        "error",
                    )

                elif role not in {
                    "branch",
                    "attestation",
                    "admin",
                }:
                    flash(
                        "Некорректная роль.",
                        "error",
                    )

                elif (
                    role == "branch"
                    and not branch_ids
                ):
                    flash(
                        "Для филиального пользователя "
                        "выберите хотя бы один филиал.",
                        "error",
                    )

                elif (
                    not full_user_name
                    or len(password) < 8
                ):
                    flash(
                        "Укажите ФИО и пароль "
                        "не короче 8 символов.",
                        "error",
                    )

                else:
                    try:
                        primary_branch = (
                            branch_ids[0]
                            if role == "branch"
                            else None
                        )

                        cursor = get_db().execute(
                            """
                            INSERT INTO users (
                                email,
                                password_hash,
                                full_name,
                                role,
                                branch_id,
                                active,
                                created_at
                            )
                            VALUES (?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                email,
                                generate_password_hash(
                                    password
                                ),
                                full_user_name,
                                role,
                                primary_branch,
                                datetime.now().isoformat(
                                    timespec="seconds"
                                ),
                            ),
                        )

                        user_id = cursor.lastrowid

                        if role == "branch":
                            for branch_id in branch_ids:
                                get_db().execute(
                                    """
                                    INSERT OR IGNORE
                                    INTO user_branch_access (
                                        user_id,
                                        branch_id
                                    )
                                    VALUES (?, ?)
                                    """,
                                    (
                                        user_id,
                                        branch_id,
                                    ),
                                )

                        get_db().commit()

                        audit(
                            "user_created",
                            details=email,
                        )

                        flash(
                            "Пользователь создан.",
                            "success",
                        )

                    except sqlite3.IntegrityError:
                        flash(
                            "Такой email уже зарегистрирован.",
                            "error",
                        )

            elif action == "edit":
                user_id = request.form.get(
                    "user_id",
                    type=int,
                )

                target = get_db().execute(
                    """
                    SELECT *
                    FROM users
                    WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()

                if not target:
                    abort(404)

                full_user_name = request.form.get(
                    "full_name",
                    "",
                ).strip()

                new_password = request.form.get(
                    "new_password",
                    "",
                )

                active = (
                    1
                    if request.form.get("active")
                    else 0
                )

                # Администратор не может отключить
                # собственную текущую учетную запись.
                if int(user_id) == int(
                    g.current_user["id"]
                ):
                    active = 1

                branch_ids = []

                for value in request.form.getlist(
                    "branch_ids"
                ):
                    try:
                        branch_id = int(value)
                    except (TypeError, ValueError):
                        continue

                    if branch_id not in branch_ids:
                        branch_ids.append(branch_id)

                if not full_user_name:
                    flash(
                        "ФИО не может быть пустым.",
                        "error",
                    )

                elif (
                    new_password
                    and len(new_password) < 8
                ):
                    flash(
                        "Новый пароль должен содержать "
                        "не менее 8 символов.",
                        "error",
                    )

                elif (
                    target["role"] == "branch"
                    and not branch_ids
                ):
                    flash(
                        "Для МУП необходимо оставить "
                        "хотя бы один филиал.",
                        "error",
                    )

                else:
                    primary_branch = (
                        branch_ids[0]
                        if target["role"] == "branch"
                        else None
                    )

                    get_db().execute(
                        """
                        UPDATE users
                        SET
                            full_name = ?,
                            branch_id = ?,
                            active = ?
                        WHERE id = ?
                        """,
                        (
                            full_user_name,
                            primary_branch,
                            active,
                            user_id,
                        ),
                    )

                    if new_password:
                        get_db().execute(
                            """
                            UPDATE users
                            SET password_hash = ?
                            WHERE id = ?
                            """,
                            (
                                generate_password_hash(
                                    new_password
                                ),
                                user_id,
                            ),
                        )

                    get_db().execute(
                        """
                        DELETE FROM user_branch_access
                        WHERE user_id = ?
                        """,
                        (user_id,),
                    )

                    if target["role"] == "branch":
                        for branch_id in branch_ids:
                            get_db().execute(
                                """
                                INSERT OR IGNORE
                                INTO user_branch_access (
                                    user_id,
                                    branch_id
                                )
                                VALUES (?, ?)
                                """,
                                (
                                    user_id,
                                    branch_id,
                                ),
                            )

                    get_db().commit()

                    audit(
                        "user_updated",
                        details=target["email"],
                    )

                    flash(
                        "Данные пользователя обновлены.",
                        "success",
                    )

            return redirect(
                url_for("admin_users")
            )

        users = get_db().execute(
            """
            SELECT
                users.*,
                branches.name AS branch_name
            FROM users
            LEFT JOIN branches
                ON branches.id = users.branch_id
            ORDER BY users.full_name
            """
        ).fetchall()

        branches = get_db().execute(
            """
            SELECT *
            FROM branches
            ORDER BY name
            """
        ).fetchall()

        access_rows = get_db().execute(
            """
            SELECT
                user_id,
                branch_id
            FROM user_branch_access
            """
        ).fetchall()

        access_map = {}

        for row in access_rows:
            access_map.setdefault(
                int(row["user_id"]),
                [],
            ).append(
                int(row["branch_id"])
            )

        body = """
        <h1>Пользователи</h1>

        <div class="card">
            <h2>Добавить пользователя</h2>

            <form method="post">
                <input
                    type="hidden"
                    name="csrf_token"
                    value="{{ csrf_token }}"
                >

                <input
                    type="hidden"
                    name="action"
                    value="create"
                >

                <div class="form-grid">
                    <div>
                        <label>ФИО</label>
                        <input
                            name="full_name"
                            required
                        >
                    </div>

                    <div>
                        <label>Email</label>
                        <input
                            type="email"
                            name="email"
                            required
                        >
                    </div>

                    <div>
                        <label>Роль</label>

                        <select
                            name="role"
                            required
                        >
                            <option value="branch">
                                Филиал / МУП
                            </option>
                            <option value="attestation">
                                Отдел аттестации
                            </option>
                            <option value="admin">
                                Администратор
                            </option>
                        </select>
                    </div>

                    <div>
                        <label>
                            Временный пароль
                        </label>

                        <input
                            type="password"
                            name="password"
                            required
                        >
                    </div>
                </div>

                <h3>Доступ к филиалам</h3>

                <div style="
                    display:grid;
                    grid-template-columns:
                        repeat(
                            auto-fill,
                            minmax(220px, 1fr)
                        );
                    gap:7px 20px;
                ">
                    {% for branch in branches %}
                        <label>
                            <input
                                type="checkbox"
                                name="branch_ids"
                                value="{{ branch.id }}"
                                style="width:auto"
                            >
                            {{ branch.name }}
                        </label>
                    {% endfor %}
                </div>

                <button
                    class="btn btn-primary space"
                    type="submit"
                >
                    Добавить пользователя
                </button>
            </form>
        </div>


        <h2 class="space">
            Существующие пользователи
        </h2>

        {% for user in users %}
            <div class="card space">
                <div style="
                    display:flex;
                    justify-content:space-between;
                    gap:20px;
                    align-items:start;
                ">
                    <div>
                        <h3 style="margin-top:0">
                            {{ user.full_name }}
                        </h3>

                        <div>
                            {{ user.email }}
                        </div>

                        <div class="muted">
                            {{ user.role }}
                            ·
                            {% if user.active %}
                                активен
                            {% else %}
                                отключен
                            {% endif %}
                        </div>
                    </div>
                </div>

                <details class="space">
                    <summary style="
                        cursor:pointer;
                        font-weight:700;
                    ">
                        Редактировать
                    </summary>

                    <form
                        method="post"
                        style="margin-top:18px"
                    >
                        <input
                            type="hidden"
                            name="csrf_token"
                            value="{{ csrf_token }}"
                        >

                        <input
                            type="hidden"
                            name="action"
                            value="edit"
                        >

                        <input
                            type="hidden"
                            name="user_id"
                            value="{{ user.id }}"
                        >

                        <div class="form-grid">
                            <div>
                                <label>Email</label>

                                <input
                                    value="{{ user.email }}"
                                    readonly
                                    style="
                                        background:#f2f2f2;
                                    "
                                >
                            </div>

                            <div>
                                <label>ФИО</label>

                                <input
                                    name="full_name"
                                    value="{{ user.full_name }}"
                                    required
                                >
                            </div>

                            <div>
                                <label>Роль</label>

                                <input
                                    value="{{ user.role }}"
                                    readonly
                                    style="
                                        background:#f2f2f2;
                                    "
                                >
                            </div>

                            <div>
                                <label>
                                    Новый пароль
                                </label>

                                <input
                                    type="password"
                                    name="new_password"
                                    placeholder="
                                        оставить без изменения
                                    "
                                >
                            </div>
                        </div>

                        {% if user.role == 'branch' %}
                            <h4>
                                Доступные филиалы
                            </h4>

                            <div style="
                                display:grid;
                                grid-template-columns:
                                    repeat(
                                        auto-fill,
                                        minmax(
                                            220px,
                                            1fr
                                        )
                                    );
                                gap:7px 20px;
                            ">
                                {% for branch in branches %}
                                    <label>
                                        <input
                                            type="checkbox"
                                            name="branch_ids"
                                            value="{{ branch.id }}"
                                            style="width:auto"
                                            {% if branch.id in access_map.get(user.id, []) %}
                                                checked
                                            {% endif %}
                                        >
                                        {{ branch.name }}
                                    </label>
                                {% endfor %}
                            </div>
                        {% endif %}

                        <p class="space">
                            <label>
                                <input
                                    type="checkbox"
                                    name="active"
                                    style="width:auto"
                                    {% if user.active %}
                                        checked
                                    {% endif %}
                                >
                                Пользователь активен
                            </label>
                        </p>

                        <button
                            class="btn btn-primary"
                            type="submit"
                        >
                            Сохранить изменения
                        </button>
                    </form>
                </details>
            </div>
        {% endfor %}
        """

        return render_page(
            "Пользователи",
            body,
            users=users,
            branches=branches,
            access_map=access_map,
        )


    app.view_functions["admin_users"] = (
        roles_required("admin")(
            admin_users_v99
        )
    )
'''


def install():
    if not APP_PATH.exists():
        raise SystemExit(
            "Не найден app.py. "
            "Запусти установщик из C:\\school-portal"
        )

    MODULE_PATH.write_text(
        MODULE_SOURCE,
        encoding="utf-8",
    )

    text = APP_PATH.read_text(
        encoding="utf-8",
    )

    import_block = (
        "\n\n"
        "from portal_review_admin_v9_9 "
        "import apply_review_admin_v9_9\n"
        "apply_review_admin_v9_9(app, globals())\n"
    )

    marker = (
        "from portal_review_admin_v9_9 "
        "import apply_review_admin_v9_9"
    )

    if marker not in text:
        launch_marker = (
            '\nif __name__ == "__main__":'
        )

        position = text.rfind(
            launch_marker
        )

        if position < 0:
            raise SystemExit(
                "В app.py не найден блок запуска."
            )

        text = (
            text[:position]
            + import_block
            + text[position:]
        )

        APP_PATH.write_text(
            text,
            encoding="utf-8",
        )

    print()
    print("Обновление v9.9 установлено.")
    print("Резервные копии не создавались.")
    print("portal.db и uploads не изменялись установщиком.")
    print()


if __name__ == "__main__":
    install()