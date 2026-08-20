from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent

APP_PATH = ROOT / "app.py"

REVIEW_MODULE = (
    ROOT / "portal_review_admin_v9_9.py"
)

FINAL_MODULE = (
    ROOT / "portal_document_consistency_v9_9_2.py"
)

MARKER = "DOCUMENT CONSISTENCY FIX V9.9.2"


# ============================================================
# Финальный runtime-патч
# ============================================================

FINAL_SOURCE = r'''
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
'''


def patch_review_module():
    if not REVIEW_MODULE.exists():
        raise RuntimeError(
            "Не найден "
            "portal_review_admin_v9_9.py"
        )

    text = REVIEW_MODULE.read_text(
        encoding="utf-8"
    )

    # ========================================================
    # 1. Документ с OCR mismatch всё равно
    #    должен отображаться как загруженный.
    # ========================================================

    old_rows = '''        rows = []

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
'''

    new_rows = '''        rows = []

        for item in checklist:
            document = item.get(
                "document"
            )

            # ------------------------------------------------
            # ВАЖНО:
            #
            # Старый checklist мог не возвращать document,
            # если OCR обнаружил несовпадение.
            #
            # Но несовпадение OCR означает
            # "ручная проверка", а НЕ
            # "документ отсутствует".
            #
            # Поэтому при отсутствии document в checklist
            # берем последнюю реально загруженную версию
            # напрямую из таблицы documents.
            # ------------------------------------------------

            if not document:

                candidate = get_db().execute(
                    """
                    SELECT *
                    FROM documents

                    WHERE student_id = ?
                      AND document_type = ?

                    ORDER BY
                        version DESC,
                        id DESC

                    LIMIT 1
                    """,
                    (
                        student["id"],
                        item["code"],
                    ),
                ).fetchone()

                if candidate:

                    stored_name = str(
                        candidate[
                            "stored_name"
                        ]
                        or ""
                    ).strip()

                    if (
                        stored_name
                        and (
                            upload_dir
                            / stored_name
                        ).exists()
                    ):
                        document = candidate

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
'''

    if new_rows not in text:

        if old_rows not in text:
            raise RuntimeError(
                "Не найден блок формирования "
                "списка документов в "
                "portal_review_admin_v9_9.py."
            )

        text = text.replace(
            old_rows,
            new_rows,
            1,
        )

    # ========================================================
    # 2. Разделяем объединённые серию + номер
    #    паспорта.
    # ========================================================

    old_passport = '''            number = (
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
'''

    new_passport = '''            number = (
                corrected.get("passport_number")
                or json_value(
                    (
                        "passport_number",
                        "number",
                    )
                )
                or card_number
            )

            # ------------------------------------------------
            # Российский паспорт:
            # серия = 4 цифры,
            # номер = 6 цифр.
            #
            # Некоторые OCR-результаты возвращают
            # один десятизначный блок:
            #
            # 7108607603
            #
            # Его необходимо интерпретировать как:
            #
            # серия 7108
            # номер 607603
            # ------------------------------------------------

            series_digits = re.sub(
                r"\\D",
                "",
                str(series or ""),
            )

            number_digits = re.sub(
                r"\\D",
                "",
                str(number or ""),
            )

            if len(number_digits) == 10:

                combined = number_digits

                if len(series_digits) != 4:
                    series_digits = (
                        combined[:4]
                    )

                if combined.startswith(
                    series_digits
                ):
                    number_digits = (
                        combined[4:]
                    )
                else:
                    number_digits = (
                        combined[-6:]
                    )

            elif len(series_digits) == 10:

                combined = series_digits

                series_digits = (
                    combined[:4]
                )

                if len(number_digits) != 6:
                    number_digits = (
                        combined[4:]
                    )

            if len(series_digits) == 4:
                series = series_digits

            if len(number_digits) == 6:
                number = number_digits

            display.extend(
'''

    if new_passport not in text:

        if old_passport not in text:
            raise RuntimeError(
                "Не найден блок паспорта "
                "в portal_review_admin_v9_9.py."
            )

        text = text.replace(
            old_passport,
            new_passport,
            1,
        )

    ast.parse(
        text,
        filename=REVIEW_MODULE.name,
    )

    REVIEW_MODULE.write_text(
        text,
        encoding="utf-8",
    )


def install_final_module():
    ast.parse(
        FINAL_SOURCE,
        filename=FINAL_MODULE.name,
    )

    FINAL_MODULE.write_text(
        FINAL_SOURCE,
        encoding="utf-8",
    )

    app_text = APP_PATH.read_text(
        encoding="utf-8"
    )

    if MARKER in app_text:
        return

    block = '''
# DOCUMENT CONSISTENCY FIX V9.9.2
from portal_document_consistency_v9_9_2 import apply_document_consistency_v9_9_2
apply_document_consistency_v9_9_2(app, globals())

'''

    launch = (
        'if __name__ == "__main__":'
    )

    position = app_text.rfind(
        launch
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

    patch_review_module()

    install_final_module()

    print()
    print(
        "Исправление v9.9.2 установлено."
    )
    print()
    print(
        "Исправлено:"
    )
    print(
        "1. Загруженный документ с "
        "OCR-расхождением больше не "
        "отображается как отсутствующий."
    )
    print(
        "2. Он остается в статусе "
        "ручной проверки."
    )
    print(
        "3. Паспортная серия и номер "
        "разделяются как 4 + 6 цифр."
    )
    print(
        "4. Очевидно объединенные "
        "паспортные данные в карточке "
        "нормализуются автоматически."
    )
    print(
        "5. Старый текст о том, что "
        "документ не закрывает обязательную "
        "позицию, убран."
    )
    print()
    print(
        "Загруженные документы не удалялись."
    )
    print(
        "Резервные копии не создавались."
    )


if __name__ == "__main__":
    main()