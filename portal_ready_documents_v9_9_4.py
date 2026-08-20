
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import (
    abort,
    flash,
    g,
    redirect,
    request,
    url_for,
)


def apply_ready_documents_v9_9_4(
    app,
    namespace,
):
    if app.config.get(
        "_READY_DOCUMENTS_V994"
    ):
        return

    app.config[
        "_READY_DOCUMENTS_V994"
    ] = True

    get_db = namespace["get_db"]

    get_student_or_404 = (
        namespace["get_student_or_404"]
    )

    roles_required = (
        namespace["roles_required"]
    )

    audit = namespace["audit"]

    document_types = (
        namespace["DOCUMENT_TYPES"]
    )

    document_is_required = (
        namespace["document_is_required"]
    )

    upload_dir = Path(
        namespace["UPLOAD_DIR"]
    )

    original_review_student = (
        app.view_functions.get(
            "review_student"
        )
    )

    if not original_review_student:
        raise RuntimeError(
            "Не найден endpoint review_student."
        )


    # ========================================================
    # Вспомогательные функции
    # ========================================================

    def config_value(
        config,
        key,
        default=None,
    ):
        try:
            return config[key]
        except Exception:
            return getattr(
                config,
                key,
                default,
            )


    def latest_physical_document(
        student_id,
        document_type,
    ):
        """
        Текущий документ = последняя версия,
        у которой реально существует файл.

        OCR status здесь НЕ участвует.
        """

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

            file_path = (
                upload_dir
                / stored_name
            )

            if file_path.exists():
                return row

        return None


    def actual_missing_documents(
        student,
    ):
        """
        Документ считается отсутствующим
        ТОЛЬКО если нет физически
        загруженной версии файла.

        OCR:
        - mismatch
        - manual_review
        - error
        - uncertain

        не делают документ отсутствующим.
        """

        missing = []

        for (
            document_type,
            config,
        ) in document_types.items():

            rule = config_value(
                config,
                "rule",
            )

            name = config_value(
                config,
                "name",
                document_type,
            )

            required = (
                document_is_required(
                    student,
                    rule,
                )
            )

            if not required:
                continue

            document = (
                latest_physical_document(
                    student["id"],
                    document_type,
                )
            )

            if not document:
                missing.append(
                    str(name)
                )

        return missing


    def can_finish_review(
        student,
    ):
        # Администратор может работать
        # с карточкой без assigned_to.
        if (
            g.current_user["role"]
            == "admin"
        ):
            return True

        # Сотрудник аттестации —
        # только со своей карточкой
        # в статусе ручной проверки.
        return (
            student["status"]
            == "in_review"

            and int(
                student["assigned_to"]
                or 0
            )
            == int(
                g.current_user["id"]
            )
        )


    # ========================================================
    # Новый финальный обработчик
    # ========================================================

    def review_student_v994(
        student_id,
    ):
        # Все обычные GET и остальные POST
        # продолжают работать через
        # существующую v9.9/v9.9.1/v9.9.3.
        if (
            request.method != "POST"
            or request.form.get(
                "action",
                ""
            ) != "ready"
        ):
            return original_review_student(
                student_id
            )

        student = get_student_or_404(
            student_id
        )

        if not can_finish_review(
            student
        ):
            abort(403)

        # -----------------------------------------------
        # Проверяем наличие документов
        # по фактическим файлам.
        # -----------------------------------------------

        missing = (
            actual_missing_documents(
                student
            )
        )

        if missing:

            flash(
                (
                    "Не загружены обязательные "
                    "документы: "
                    + ", ".join(missing)
                ),
                "error",
            )

            return redirect(
                url_for(
                    "review_student",
                    student_id=student_id,
                )
            )

        # -----------------------------------------------
        # Открытые замечания по-прежнему блокируют
        # перевод в «Готово к зачислению».
        # -----------------------------------------------

        open_comments = (
            get_db().execute(
                """
                SELECT COUNT(*)
                FROM comments

                WHERE student_id = ?
                  AND is_open = 1
                """,
                (
                    student_id,
                ),
            ).fetchone()[0]
        )

        if open_comments:

            flash(
                (
                    "Карточку нельзя передать "
                    "на зачисление: есть "
                    "открытые замечания."
                ),
                "error",
            )

            return redirect(
                url_for(
                    "review_student",
                    student_id=student_id,
                )
            )

        # -----------------------------------------------
        # Все обязательные файлы есть.
        #
        # OCR mismatch здесь НЕ блокирует переход.
        # Сотрудник уже выполняет ручную проверку.
        # -----------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        get_db().execute(
            """
            UPDATE students

            SET
                status = 'ready',
                updated_at = ?

            WHERE id = ?
            """,
            (
                now,
                student_id,
            ),
        )

        get_db().commit()

        audit(
            "student_ready",
            student_id,
            (
                "Все обязательные документы "
                "проверены по фактическому "
                "наличию файлов. "
                "OCR-расхождения не блокируют "
                "готовность к зачислению."
            ),
        )

        flash(
            (
                "Карточка готова к зачислению."
            ),
            "success",
        )

        return redirect(
            url_for(
                "review_student",
                student_id=student_id,
            )
        )


    wrapped = roles_required(
        "attestation",
        "admin",
    )(
        review_student_v994
    )

    wrapped.__name__ = (
        "review_student_v994"
    )

    app.view_functions[
        "review_student"
    ] = wrapped
