
from __future__ import annotations

import re

from flask import request


def apply_document_consistency_v9_9_2(
    app,
    namespace,
):
    if app.config.get(
        "_DOCUMENT_CONSISTENCY_V992"
    ):
        return

    app.config[
        "_DOCUMENT_CONSISTENCY_V992"
    ] = True

    get_db = namespace["get_db"]


    # ========================================================
    # Паспорт РФ:
    #
    # серия = 4 цифры
    # номер = 6 цифр
    # ========================================================

    def digits(value):
        return re.sub(
            r"\D",
            "",
            str(value or ""),
        )


    def normalize_pair(
        series,
        number,
    ):
        series_text = str(
            series or ""
        ).strip()

        number_text = str(
            number or ""
        ).strip()

        s = digits(series_text)
        n = digits(number_text)

        changed = False

        # --------------------------------------------
        # Типичная OCR-ошибка:
        #
        # series = 7108
        # number = 7108607603
        #
        # или:
        #
        # series = ""
        # number = 7108607603
        # --------------------------------------------

        if len(n) == 10:

            combined = n

            if len(s) != 4:
                s = combined[:4]
                changed = True

            if (
                len(s) == 4
                and combined.startswith(s)
            ):
                n = combined[4:]
            else:
                n = combined[-6:]

            changed = True

        # Иногда весь блок может попасть
        # наоборот в поле серии.
        if len(s) == 10:

            combined = s

            s = combined[:4]

            if len(n) != 6:
                n = combined[4:]

            changed = True

        # Если после нормализации значения
        # имеют правильную длину,
        # приводим их к чистым цифрам.
        if len(s) == 4:
            if series_text != s:
                changed = True

            series_text = s

        if len(n) == 6:
            if number_text != n:
                changed = True

            number_text = n

        return (
            series_text,
            number_text,
            changed,
        )


    def normalize_student_passports(
        student_id,
    ):
        if not student_id:
            return

        student = get_db().execute(
            """
            SELECT
                id,
                parent_passport_series,
                parent_passport_number,
                child_passport_series,
                child_passport_number

            FROM students
            WHERE id = ?
            """,
            (
                student_id,
            ),
        ).fetchone()

        if not student:
            return

        changed = False

        (
            parent_series,
            parent_number,
            parent_changed,
        ) = normalize_pair(
            student[
                "parent_passport_series"
            ],
            student[
                "parent_passport_number"
            ],
        )

        (
            child_series,
            child_number,
            child_changed,
        ) = normalize_pair(
            student[
                "child_passport_series"
            ],
            student[
                "child_passport_number"
            ],
        )

        if parent_changed:
            get_db().execute(
                """
                UPDATE students

                SET
                    parent_passport_series = ?,
                    parent_passport_number = ?

                WHERE id = ?
                """,
                (
                    parent_series,
                    parent_number,
                    student_id,
                ),
            )

            changed = True

        if child_changed:
            get_db().execute(
                """
                UPDATE students

                SET
                    child_passport_series = ?,
                    child_passport_number = ?

                WHERE id = ?
                """,
                (
                    child_series,
                    child_number,
                    student_id,
                ),
            )

            changed = True

        if changed:
            get_db().commit()


    @app.after_request
    def normalize_passports_after_request(
        response,
    ):
        """
        После OCR / замены документа автоматически
        исправляем очевидное объединение
        серии и номера паспорта.
        """

        try:
            view_args = (
                request.view_args
                or {}
            )

            student_id = view_args.get(
                "student_id"
            )

            if student_id:
                normalize_student_passports(
                    int(student_id)
                )

        except Exception:
            # Ошибка нормализации не должна
            # ломать основной запрос.
            pass


        # ----------------------------------------------------
        # Старый текст предварительной проверки говорил,
        # что документ не закрывает обязательную позицию.
        #
        # Это больше не соответствует новой логике.
        # ----------------------------------------------------

        try:
            if (
                response.status_code == 200
                and response.mimetype
                    == "text/html"
            ):
                body = response.get_data(
                    as_text=True
                )

                old_phrases = (
                    (
                        "и до решения проверяющего "
                        "не будет закрывать обязательную "
                        "позицию.",
                        (
                            "и будет передан сотруднику "
                            "аттестации на ручную проверку."
                        ),
                    ),
                    (
                        "не будет закрывать "
                        "обязательную позицию",
                        (
                            "будет передан на "
                            "ручную проверку"
                        ),
                    ),
                )

                for old, new in old_phrases:
                    body = body.replace(
                        old,
                        new,
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

        except Exception:
            pass

        return response
