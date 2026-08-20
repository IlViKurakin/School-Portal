from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_PATH = ROOT / "app.py"

MODULE_PATH = (
    ROOT / "portal_workflow_fix_v9_9_1.py"
)

MARKER = "WORKFLOW FIX V9.9.1"


MODULE_SOURCE = r'''
from __future__ import annotations

import re
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


def apply_workflow_fix_v9_9_1(
    app,
    namespace,
):
    if app.config.get(
        "_WORKFLOW_FIX_V991"
    ):
        return

    app.config[
        "_WORKFLOW_FIX_V991"
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

    comment_categories = (
        namespace["COMMENT_CATEGORIES"]
    )

    upload_dir = Path(
        namespace["UPLOAD_DIR"]
    )


    # ========================================================
    # 1. Наличие документа и результат OCR —
    #    это ДВЕ разные вещи.
    # ========================================================

    def latest_documents(
        student_id,
    ):
        rows = get_db().execute(
            """
            SELECT d.*
            FROM documents d

            JOIN (
                SELECT
                    document_type,
                    MAX(version) AS max_version

                FROM documents

                WHERE student_id = ?

                GROUP BY document_type
            ) latest
                ON latest.document_type
                    = d.document_type

               AND latest.max_version
                    = d.version

            WHERE d.student_id = ?
            """,
            (
                student_id,
                student_id,
            ),
        ).fetchall()

        result = {}

        for row in rows:
            result[
                row["document_type"]
            ] = row

        return result


    def document_physically_exists(
        document,
    ):
        if not document:
            return False

        stored_name = str(
            document["stored_name"]
            or ""
        ).strip()

        if not stored_name:
            return False

        return (
            upload_dir / stored_name
        ).exists()


    def actual_document_state(
        student,
    ):
        """
        Комплектность определяется только
        фактом существования загруженного файла.

        OCR-расхождение НЕ превращает
        загруженный документ в отсутствующий.
        """

        latest = latest_documents(
            student["id"]
        )

        required_count = 0
        uploaded_count = 0
        missing = []

        for code, config in (
            document_types.items()
        ):
            required = (
                document_is_required(
                    student,
                    config["rule"],
                )
            )

            if not required:
                continue

            required_count += 1

            document = latest.get(code)

            if document_physically_exists(
                document
            ):
                uploaded_count += 1

            else:
                missing.append(
                    config["name"]
                )

        return (
            required_count,
            uploaded_count,
            missing,
        )


    # ========================================================
    # 2. Отправка МУПом
    #
    #    OCR mismatch / manual review НЕ блокирует.
    # ========================================================

    def submit_student_v991(
        student_id,
    ):
        student = get_student_or_404(
            student_id
        )

        if student["status"] not in (
            "draft",
            "correction",
        ):
            abort(400)

        (
            required_count,
            uploaded_count,
            missing,
        ) = actual_document_state(
            student
        )

        if missing:
            flash(
                (
                    "Нельзя отправить комплект. "
                    "Не загружены: "
                    + ", ".join(missing)
                ),
                "error",
            )

            return redirect(
                url_for(
                    "student_detail",
                    student_id=student_id,
                )
            )

        # Открытые замечания сотрудника
        # аттестации по-прежнему должны
        # быть обработаны МУПом.
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
                    "Сначала отметьте "
                    "выполненными все открытые "
                    "замечания."
                ),
                "error",
            )

            return redirect(
                url_for(
                    "student_detail",
                    student_id=student_id,
                )
            )

        get_db().execute(
            """
            UPDATE students

            SET
                status = 'submitted',
                assigned_to = NULL,
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
            "student_submitted",
            student_id,
            (
                f"documents="
                f"{uploaded_count}/"
                f"{required_count}; "
                "OCR mismatches allowed "
                "for manual review"
            ),
        )

        flash(
            (
                "Карточка передана "
                "в отдел аттестации. "
                "Документы с расхождениями "
                "OCR будут проверены вручную."
            ),
            "success",
        )

        return redirect(
            url_for(
                "student_detail",
                student_id=student_id,
            )
        )


    app.view_functions[
        "submit_student"
    ] = roles_required(
        "branch"
    )(
        submit_student_v991
    )


    # ========================================================
    # 3. Исправляем счетчик комплектности 8/9 -> 9/9
    #
    #    Оборачиваем уже существующую страницу,
    #    не переписывая ее.
    # ========================================================

    original_student_detail = (
        app.view_functions.get(
            "student_detail"
        )
    )

    if original_student_detail:

        def student_detail_v991(
            student_id,
        ):
            result = (
                original_student_detail(
                    student_id
                )
            )

            response = (
                app.make_response(
                    result
                )
            )

            if (
                request.method != "GET"
                or response.status_code != 200
                or response.mimetype
                    != "text/html"
            ):
                return response

            student = (
                get_student_or_404(
                    student_id
                )
            )

            (
                required_count,
                uploaded_count,
                missing,
            ) = actual_document_state(
                student
            )

            try:
                body = response.get_data(
                    as_text=True
                )
            except Exception:
                return response

            # Меняем только число внутри
            # блока «Комплектность».
            pattern = re.compile(
                r"("
                r"Комплектность"
                r".{0,1200}?"
                r'class=["\'][^"\']*'
                r'metric-number[^"\']*'
                r'["\'][^>]*>'
                r"\s*"
                r")"
                r"\d+\s*/\s*\d+",
                re.IGNORECASE
                | re.DOTALL,
            )

            body, count = (
                pattern.subn(
                    lambda match:
                        (
                            match.group(1)
                            + str(
                                uploaded_count
                            )
                            + " / "
                            + str(
                                required_count
                            )
                        ),
                    body,
                    count=1,
                )
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

        app.view_functions[
            "student_detail"
        ] = student_detail_v991


    # ========================================================
    # 4. Замечания аттестации:
    #
    #    выбранной категории достаточно.
    # ========================================================

    original_review_student = (
        app.view_functions.get(
            "review_student"
        )
    )

    if not original_review_student:
        return


    def can_edit_review(
        student,
    ):
        if (
            g.current_user["role"]
            == "admin"
        ):
            return True

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


    def create_comment(
        student,
    ):
        if not can_edit_review(
            student
        ):
            abort(403)

        category = request.form.get(
            "category",
            "",
        ).strip()

        document_type = (
            request.form.get(
                "document_type"
            )
            or None
        )

        text = request.form.get(
            "text",
            "",
        ).strip()

        if (
            category
            not in comment_categories
        ):
            flash(
                "Выберите причину замечания.",
                "error",
            )

            return False

        if (
            document_type
            and document_type
            not in document_types
        ):
            flash(
                "Некорректный тип документа.",
                "error",
            )

            return False

        # Для «Другое» свободный текст
        # действительно нужен.
        if (
            category == "Другое"
            and not text
        ):
            flash(
                (
                    "Для категории «Другое» "
                    "укажите комментарий."
                ),
                "error",
            )

            return False

        # Для остальных категорий ручной
        # текст НЕ обязателен.
        if not text:
            text = category

        get_db().execute(
            """
            INSERT INTO comments (
                student_id,
                document_type,
                category,
                text,
                is_open,
                created_by,
                created_at
            )

            VALUES (
                ?, ?, ?, ?, 1, ?, ?
            )
            """,
            (
                student["id"],
                document_type,
                category,
                text,
                g.current_user["id"],
                datetime.now().isoformat(
                    timespec="seconds"
                ),
            ),
        )

        get_db().commit()

        audit(
            "comment_created",
            student["id"],
            (
                f"{category}: {text}"
            ),
        )

        return True


    def return_student(
        student,
    ):
        get_db().execute(
            """
            UPDATE students

            SET
                status = 'correction',
                assigned_to = NULL,
                updated_at = ?

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
            "student_returned",
            student["id"],
        )


    def review_student_v991(
        student_id,
    ):
        student = get_student_or_404(
            student_id
        )

        if request.method == "POST":
            action = request.form.get(
                "action",
                "",
            )

            # -------------------------------------------
            # Просто добавить замечание.
            # -------------------------------------------

            if action == "comment":

                if create_comment(
                    student
                ):
                    flash(
                        "Замечание добавлено.",
                        "success",
                    )

                return redirect(
                    url_for(
                        "review_student",
                        student_id=student_id,
                    )
                )

            # -------------------------------------------
            # Одновременно:
            # причина + вернуть на исправление.
            # -------------------------------------------

            if action == (
                "return_with_comment"
            ):

                if not create_comment(
                    student
                ):
                    return redirect(
                        url_for(
                            "review_student",
                            student_id=
                                student_id,
                        )
                    )

                return_student(
                    student
                )

                flash(
                    (
                        "Замечание сохранено. "
                        "Карточка возвращена "
                        "филиалу на исправление."
                    ),
                    "success",
                )

                return redirect(
                    url_for(
                        "review_queue"
                    )
                )

            # -------------------------------------------
            # Старый вариант возврата.
            #
            # Оставляем для совместимости,
            # если уже есть открытые замечания.
            # -------------------------------------------

            if action == "return":

                if not can_edit_review(
                    student
                ):
                    abort(403)

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

                if not open_comments:
                    flash(
                        (
                            "Сначала выберите "
                            "причину замечания."
                        ),
                        "error",
                    )

                    return redirect(
                        url_for(
                            "review_student",
                            student_id=
                                student_id,
                        )
                    )

                return_student(
                    student
                )

                flash(
                    (
                        "Карточка возвращена "
                        "филиалу."
                    ),
                    "success",
                )

                return redirect(
                    url_for(
                        "review_queue"
                    )
                )

        # Остальные действия:
        # take, ready, enroll, save_ocr
        # остаются в действующей версии v9.9.
        result = (
            original_review_student(
                student_id
            )
        )

        response = (
            app.make_response(
                result
            )
        )

        if (
            request.method != "GET"
            or response.status_code != 200
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


        # ====================================================
        # 5. Поле комментария становится необязательным.
        # ====================================================

        textarea_pattern = re.compile(
            r"<textarea\b"
            r"[^>]*"
            r'name=["\']text["\']'
            r"[^>]*>",
            re.IGNORECASE,
        )

        def fix_textarea(
            match,
        ):
            tag = match.group(0)

            tag = re.sub(
                r"\s+required"
                r'(?:=["\']required["\'])?',
                "",
                tag,
                flags=re.IGNORECASE,
            )

            return tag

        body = textarea_pattern.sub(
            fix_textarea,
            body,
            count=1,
        )

        body = body.replace(
            "Комментарий</label>",
            (
                "Дополнительный комментарий "
                "(необязательно)</label>"
            ),
            1,
        )


        # ====================================================
        # 6. Убираем hidden action=comment.
        #
        #    Теперь действие определяется кнопкой.
        # ====================================================

        body = re.sub(
            r"<input\b"
            r"(?=[^>]*"
            r'name=["\']action["\'])'
            r"(?=[^>]*"
            r'value=["\']comment["\'])'
            r"[^>]*>",
            "",
            body,
            count=1,
            flags=re.IGNORECASE,
        )


        # ====================================================
        # 7. Две понятные кнопки:
        #
        #    - добавить замечание;
        #    - сразу вернуть карточку.
        # ====================================================

        add_button_pattern = re.compile(
            r"<button\b"
            r"[^>]*"
            r">\s*"
            r"Добавить замечание"
            r"\s*</button>",
            re.IGNORECASE,
        )

        replacement_buttons = """
        <div style="
            display:flex;
            gap:10px;
            flex-wrap:wrap;
            margin-top:18px;
        ">
            <button
                class="btn btn-secondary"
                type="submit"
                name="action"
                value="comment"
            >
                Добавить замечание
            </button>

            <button
                class="btn btn-primary"
                type="submit"
                name="action"
                value="return_with_comment"
            >
                Вернуть на исправление
            </button>
        </div>
        """

        body = add_button_pattern.sub(
            replacement_buttons,
            body,
            count=1,
        )


        # ====================================================
        # 8. Старую отдельную кнопку возврата
        #    убираем из интерфейса.
        #
        #    Backend при этом ее еще понимает.
        # ====================================================

        old_return_form = re.compile(
            r"<form\b"
            r"[^>]*"
            r'class=["\'][^"\']*'
            r'inline[^"\']*["\']'
            r"[^>]*>"
            r"(?:(?!</form>).)*?"
            r'name=["\']action["\']'
            r"(?:(?!</form>).)*?"
            r'value=["\']return["\']'
            r"(?:(?!</form>).)*?"
            r"Вернуть на исправление"
            r"(?:(?!</form>).)*?"
            r"</form>",
            re.IGNORECASE
            | re.DOTALL,
        )

        body = old_return_form.sub(
            "",
            body,
            count=1,
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


    app.view_functions[
        "review_student"
    ] = roles_required(
        "attestation",
        "admin",
    )(
        review_student_v991
    )
'''


def main():
    if not APP_PATH.exists():
        raise RuntimeError(
            "Не найден C:\\school-portal\\app.py"
        )

    # ---------------------------------------------
    # Создаём финальный модуль.
    # ---------------------------------------------

    ast.parse(
        MODULE_SOURCE,
        filename=MODULE_PATH.name,
    )

    MODULE_PATH.write_text(
        MODULE_SOURCE,
        encoding="utf-8",
    )

    app_text = APP_PATH.read_text(
        encoding="utf-8"
    )

    if MARKER in app_text:
        print(
            "Workflow fix v9.9.1 "
            "уже подключен."
        )
        return

    block = """
# WORKFLOW FIX V9.9.1
from portal_workflow_fix_v9_9_1 import apply_workflow_fix_v9_9_1
apply_workflow_fix_v9_9_1(app, globals())

"""

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

    # ВАЖНО:
    # применяем самым последним,
    # чтобы старые validation-патчи
    # больше не возвращали старое поведение.
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

    print()
    print(
        "Workflow fix v9.9.1 установлен."
    )
    print()
    print(
        "Исправлено:"
    )
    print(
        "1. OCR-расхождение больше "
        "не делает документ отсутствующим."
    )
    print(
        "2. Такой документ можно "
        "отправить в аттестацию."
    )
    print(
        "3. Комплектность считается "
        "по фактически загруженным файлам."
    )
    print(
        "4. Ручной комментарий "
        "к замечанию необязателен."
    )
    print(
        "5. Причину можно выбрать "
        "из списка и сразу вернуть карточку."
    )
    print()
    print(
        "База и загруженные документы "
        "не удалялись."
    )
    print(
        "Резервные копии не создавались."
    )


if __name__ == "__main__":
    main()