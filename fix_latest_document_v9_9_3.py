from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent

APP_PATH = ROOT / "app.py"

REVIEW_MODULE = (
    ROOT / "portal_review_admin_v9_9.py"
)

RUNTIME_MODULE = (
    ROOT / "portal_latest_document_v9_9_3.py"
)

MARKER = "LATEST DOCUMENT FIX V9.9.3"


RUNTIME_SOURCE = r'''
from __future__ import annotations

import re
from pathlib import Path

from flask import request


def apply_latest_document_v9_9_3(
    app,
    namespace,
):
    if app.config.get(
        "_LATEST_DOCUMENT_V993"
    ):
        return

    app.config[
        "_LATEST_DOCUMENT_V993"
    ] = True

    get_db = namespace["get_db"]

    upload_dir = Path(
        namespace["UPLOAD_DIR"]
    )

    original_build_document_checklist = (
        namespace[
            "build_document_checklist"
        ]
    )


    # ========================================================
    # Последний реально существующий файл документа
    # ========================================================

    def latest_physical_document(
        student_id,
        document_type,
    ):
        rows = get_db().execute(
            """
            SELECT *
            FROM documents

            WHERE student_id = ?
              AND document_type = ?

            ORDER BY
                version DESC,
                id DESC
            """,
            (
                student_id,
                document_type,
            ),
        ).fetchall()

        for row in rows:

            stored_name = str(
                row["stored_name"]
                or ""
            ).strip()

            if not stored_name:
                continue

            path = (
                upload_dir
                / stored_name
            )

            if path.exists():
                return row

        return None


    # ========================================================
    # Главная карточка МУП.
    #
    # Старый checklist больше не имеет права
    # скрывать документ из-за OCR mismatch.
    # ========================================================

    def build_document_checklist_v993(
        student,
    ):
        checklist = (
            original_build_document_checklist(
                student
            )
        )

        result = []

        for item in checklist:

            new_item = dict(item)

            document_type = (
                new_item.get("code")
            )

            if document_type:

                current_document = (
                    latest_physical_document(
                        student["id"],
                        document_type,
                    )
                )

                # ВАЖНО:
                # наличие определяется только файлом.
                new_item["document"] = (
                    current_document
                )

            result.append(
                new_item
            )

        return result


    # Меняем функцию непосредственно
    # в globals app.py.
    namespace[
        "build_document_checklist"
    ] = build_document_checklist_v993


    # ========================================================
    # Старое пояснение на странице OCR
    # больше не соответствует бизнес-логике.
    # ========================================================

    @app.after_request
    def v993_fix_manual_review_text(
        response,
    ):
        if (
            response.status_code != 200
            or response.mimetype
                != "text/html"
        ):
            return response

        try:
            body = response.get_data(
                as_text=True
            )

        except Exception:
            return response

        body = re.sub(
            (
                r"до решения проверяющего"
                r"\s+не будет закрывать"
                r"\s+обязательную"
                r"\s+позицию"
            ),
            (
                "будет передан сотруднику "
                "аттестации на ручную проверку"
            ),
            body,
            flags=re.IGNORECASE,
        )

        body = re.sub(
            (
                r"не будет закрывать"
                r"\s+обязательную"
                r"\s+позицию"
            ),
            (
                "будет передан на "
                "ручную проверку"
            ),
            body,
            flags=re.IGNORECASE,
        )

        response.set_data(
            body
        )

        response.headers[
            "Content-Length"
        ] = str(
            len(
                response.get_data()
            )
        )

        return response
'''


def patch_attestation_screen():
    if not REVIEW_MODULE.exists():
        raise RuntimeError(
            "Не найден "
            "portal_review_admin_v9_9.py"
        )

    text = REVIEW_MODULE.read_text(
        encoding="utf-8"
    )

    loop_position = text.find(
        "        for item in checklist:"
    )

    if loop_position < 0:
        raise RuntimeError(
            "Не найден цикл документов "
            "в portal_review_admin_v9_9.py."
        )

    document_position = text.find(
        "            document = item.get(",
        loop_position,
    )

    if document_position < 0:
        raise RuntimeError(
            "Не найден выбор document "
            "в portal_review_admin_v9_9.py."
        )

    display_position = text.find(
        "            display = []",
        document_position,
    )

    if display_position < 0:
        raise RuntimeError(
            "Не найден блок display "
            "в portal_review_admin_v9_9.py."
        )

    replacement = '''            # ==========================================
            # Всегда выбираем ПОСЛЕДНЮЮ
            # физически существующую версию.
            #
            # OCR mismatch / manual review
            # не должен возвращать старый файл.
            # ==========================================

            document = None

            candidates = get_db().execute(
                """
                SELECT *
                FROM documents

                WHERE student_id = ?
                  AND document_type = ?

                ORDER BY
                    version DESC,
                    id DESC
                """,
                (
                    student["id"],
                    item["code"],
                ),
            ).fetchall()

            for candidate in candidates:

                stored_name = str(
                    candidate[
                        "stored_name"
                    ]
                    or ""
                ).strip()

                if not stored_name:
                    continue

                if (
                    upload_dir
                    / stored_name
                ).exists():

                    document = candidate
                    break

'''

    text = (
        text[:document_position]
        + replacement
        + text[display_position:]
    )

    ast.parse(
        text,
        filename=REVIEW_MODULE.name,
    )

    REVIEW_MODULE.write_text(
        text,
        encoding="utf-8",
    )


def install_runtime_patch():
    ast.parse(
        RUNTIME_SOURCE,
        filename=RUNTIME_MODULE.name,
    )

    RUNTIME_MODULE.write_text(
        RUNTIME_SOURCE,
        encoding="utf-8",
    )

    app_text = APP_PATH.read_text(
        encoding="utf-8"
    )

    if MARKER in app_text:
        return

    block = '''
# LATEST DOCUMENT FIX V9.9.3
from portal_latest_document_v9_9_3 import apply_latest_document_v9_9_3
apply_latest_document_v9_9_3(app, globals())

'''

    launch_marker = (
        'if __name__ == "__main__":'
    )

    position = app_text.rfind(
        launch_marker
    )

    if position < 0:
        raise RuntimeError(
            "В app.py не найден блок запуска."
        )

    app_text = (
        app_text[:position]
        + block
        + app_text[position:]
    )

    ast.parse(
        app_text,
        filename="app.py",
    )

    APP_PATH.write_text(
        app_text,
        encoding="utf-8",
    )


def main():
    if not APP_PATH.exists():
        raise RuntimeError(
            "Не найден C:\\school-portal\\app.py"
        )

    patch_attestation_screen()

    install_runtime_patch()

    print()
    print(
        "Исправление v9.9.3 установлено."
    )
    print()
    print(
        "Теперь:"
    )
    print(
        "1. Текущим считается последняя "
        "реально загруженная версия документа."
    )
    print(
        "2. OCR-расхождение не скрывает файл."
    )
    print(
        "3. МУП видит последний загруженный файл."
    )
    print(
        "4. Аттестация видит именно последний файл."
    )
    print(
        "5. Старые версии остаются в истории."
    )
    print(
        "6. Ручная проверка не влияет "
        "на комплектность."
    )
    print()
    print(
        "portal.db не очищалась."
    )
    print(
        "uploads не очищалась."
    )
    print(
        "Резервные копии не создавались."
    )


if __name__ == "__main__":
    main()