
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
