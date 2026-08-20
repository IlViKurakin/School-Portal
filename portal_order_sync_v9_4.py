
from __future__ import annotations

import html
import re
import sqlite3

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    g,
    request,
)


def apply_order_sync_v9_4(
    app,
    namespace: dict[str, Any],
) -> None:

    # Защита от повторного подключения
    # в одном процессе Flask.
    if app.extensions.get(
        "order_sync_v9_4"
    ):
        return

    app.extensions[
        "order_sync_v9_4"
    ] = True

    database_path: Path = namespace[
        "DATABASE_PATH"
    ]

    audit = namespace.get(
        "audit"
    )

    # ========================================================
    # БАЗА
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


    def table_exists(
        connection,
        table_name: str,
    ) -> bool:

        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE
                type = 'table'
                AND name = ?
            """,
            (
                table_name,
            ),
        ).fetchone()

        return row is not None


    def migrate():

        connection = connect()

        try:

            if not table_exists(
                connection,
                "post_enrollment_documents",
            ):
                raise RuntimeError(
                    "Не найдена таблица "
                    "post_enrollment_documents. "
                    "Сначала должна быть "
                    "установлена v8."
                )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                student_enrollment_orders (

                    student_id INTEGER
                        PRIMARY KEY,

                    order_number TEXT
                        NOT NULL,

                    order_date TEXT
                        NOT NULL,

                    updated_by INTEGER,

                    updated_at TEXT
                        NOT NULL,

                    FOREIGN KEY (
                        student_id
                    )
                    REFERENCES students(id)
                )
                """
            )

            connection.commit()

            # --------------------------------
            # Подхватываем уже существующие
            # реквизиты приказов.
            # --------------------------------

            student_rows = (
                connection.execute(
                    """
                    SELECT DISTINCT
                        student_id

                    FROM
                        post_enrollment_documents

                    WHERE
                        trim(
                            coalesce(
                                order_number,
                                ''
                            )
                        ) <> ''

                        AND

                        trim(
                            coalesce(
                                order_date,
                                ''
                            )
                        ) <> ''
                    """
                ).fetchall()
            )

            for student_row in student_rows:

                student_id = (
                    student_row[
                        "student_id"
                    ]
                )

                existing_shared = (
                    connection.execute(
                        """
                        SELECT *
                        FROM
                            student_enrollment_orders
                        WHERE student_id = ?
                        """,
                        (
                            student_id,
                        ),
                    ).fetchone()
                )

                if existing_shared:
                    continue

                # При наличии нескольких
                # старых вариантов берем
                # последний окончательно
                # сформированный документ.
                #
                # Если выданных документов
                # нет — последний измененный
                # черновик.
                source = (
                    connection.execute(
                        """
                        SELECT
                            order_number,
                            order_date,
                            finalized_by,
                            updated_by,
                            finalized_at,
                            updated_at,
                            created_at,
                            status

                        FROM
                            post_enrollment_documents

                        WHERE
                            student_id = ?

                            AND
                            trim(
                                coalesce(
                                    order_number,
                                    ''
                                )
                            ) <> ''

                            AND
                            trim(
                                coalesce(
                                    order_date,
                                    ''
                                )
                            ) <> ''

                        ORDER BY
                            CASE
                                WHEN status =
                                    'issued'
                                THEN 1
                                ELSE 0
                            END DESC,

                            coalesce(
                                finalized_at,
                                updated_at,
                                created_at
                            ) DESC

                        LIMIT 1
                        """,
                        (
                            student_id,
                        ),
                    ).fetchone()
                )

                if not source:
                    continue

                timestamp = (
                    datetime.now()
                    .isoformat(
                        timespec="seconds"
                    )
                )

                connection.execute(
                    """
                    INSERT INTO
                        student_enrollment_orders
                    (
                        student_id,
                        order_number,
                        order_date,
                        updated_by,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        student_id,

                        source[
                            "order_number"
                        ],

                        source[
                            "order_date"
                        ],

                        (
                            source[
                                "finalized_by"
                            ]
                            or source[
                                "updated_by"
                            ]
                        ),

                        timestamp,
                    ),
                )

            connection.commit()

            # --------------------------------
            # Сразу синхронизируем
            # существующие ЧЕРНОВИКИ.
            #
            # Уже выданные документы
            # не изменяем.
            # --------------------------------

            connection.execute(
                """
                UPDATE
                    post_enrollment_documents

                SET
                    order_number = (
                        SELECT
                            seo.order_number

                        FROM
                            student_enrollment_orders
                            seo

                        WHERE
                            seo.student_id =
                            post_enrollment_documents
                            .student_id
                    ),

                    order_date = (
                        SELECT
                            seo.order_date

                        FROM
                            student_enrollment_orders
                            seo

                        WHERE
                            seo.student_id =
                            post_enrollment_documents
                            .student_id
                    )

                WHERE
                    status = 'draft'

                    AND EXISTS (
                        SELECT 1
                        FROM
                            student_enrollment_orders
                            seo

                        WHERE
                            seo.student_id =
                            post_enrollment_documents
                            .student_id
                    )
                """
            )

            connection.commit()

        finally:
            connection.close()


    migrate()

    # ========================================================
    # ЕДИНЫЕ ДАННЫЕ ПРИКАЗА
    # ========================================================

    def get_shared_order(
        student_id: int,
    ):

        connection = connect()

        try:
            return connection.execute(
                """
                SELECT
                    order_number,
                    order_date

                FROM
                    student_enrollment_orders

                WHERE student_id = ?
                """,
                (
                    student_id,
                ),
            ).fetchone()

        finally:
            connection.close()


    def save_shared_order(
        student_id: int,
        order_number: str,
        order_date: str,
    ) -> bool:

        order_number = (
            order_number
            .strip()
        )

        order_date = (
            order_date
            .strip()
        )

        if (
            not order_number
            or not order_date
        ):
            return False

        # Проверка даты.
        try:
            datetime.strptime(
                order_date,
                "%Y-%m-%d",
            )

        except ValueError:
            # Основной обработчик формы
            # сам покажет пользователю
            # ошибку.
            return False

        connection = connect()

        try:

            previous = (
                connection.execute(
                    """
                    SELECT
                        order_number,
                        order_date

                    FROM
                        student_enrollment_orders

                    WHERE student_id = ?
                    """,
                    (
                        student_id,
                    ),
                ).fetchone()
            )

            timestamp = (
                datetime.now()
                .isoformat(
                    timespec="seconds"
                )
            )

            user_id = None

            try:
                user_id = (
                    g.current_user[
                        "id"
                    ]
                )
            except Exception:
                pass

            connection.execute(
                """
                INSERT INTO
                    student_enrollment_orders
                (
                    student_id,
                    order_number,
                    order_date,
                    updated_by,
                    updated_at
                )

                VALUES (?, ?, ?, ?, ?)

                ON CONFLICT(student_id)
                DO UPDATE SET

                    order_number =
                        excluded.order_number,

                    order_date =
                        excluded.order_date,

                    updated_by =
                        excluded.updated_by,

                    updated_at =
                        excluded.updated_at
                """,
                (
                    student_id,
                    order_number,
                    order_date,
                    user_id,
                    timestamp,
                ),
            )

            # --------------------------------
            # Синхронизируем второй документ,
            # если его черновик уже создан.
            #
            # Финальные документы не трогаем.
            # --------------------------------

            connection.execute(
                """
                UPDATE
                    post_enrollment_documents

                SET
                    order_number = ?,
                    order_date = ?,
                    updated_at = ?,

                    updated_by =
                        coalesce(
                            ?,
                            updated_by
                        )

                WHERE
                    student_id = ?
                    AND status = 'draft'
                """,
                (
                    order_number,
                    order_date,
                    timestamp,
                    user_id,
                    student_id,
                ),
            )

            connection.commit()

            changed = (
                previous is None
                or previous[
                    "order_number"
                ] != order_number
                or previous[
                    "order_date"
                ] != order_date
            )

            if (
                changed
                and audit
            ):

                try:
                    audit(
                        "enrollment_order_synced",
                        student_id,
                        (
                            "Реквизиты приказа: "
                            f"№ {order_number}, "
                            f"дата {order_date}"
                        ),
                    )

                except Exception:
                    pass

            return True

        finally:
            connection.close()

    # ========================================================
    # ОПРЕДЕЛЕНИЕ УЧЕНИКА
    # ========================================================

    def current_student_id():

        view_args = (
            request.view_args
            or {}
        )

        value = view_args.get(
            "student_id"
        )

        if value is not None:
            try:
                return int(value)
            except (
                TypeError,
                ValueError,
            ):
                pass

        # Запасной вариант —
        # берем ID из URL.
        match = re.search(
            r"/students/(\d+)/",
            request.path,
        )

        if match:
            return int(
                match.group(1)
            )

        return None


    def is_post_document_page():

        path = (
            request.path
            .lower()
        )

        # Ограничиваем работу только
        # разделом документов после
        # зачисления.
        return (
            "post-doc" in path
            or "order-extract" in path
            or "certificate" in path
        )

    # ========================================================
    # POST:
    # СОХРАНЯЕМ РЕКВИЗИТЫ ИЗ ЛЮБОГО
    # ИЗ ДВУХ ДОКУМЕНТОВ
    # ========================================================

    @app.before_request
    def order_sync_before_request():

        if request.method != "POST":
            return None

        if not is_post_document_page():
            return None

        student_id = (
            current_student_id()
        )

        if not student_id:
            return None

        if (
            "order_number"
            not in request.form
            and "order_date"
            not in request.form
        ):
            return None

        order_number = (
            request.form.get(
                "order_number",
                "",
            )
        )

        order_date = (
            request.form.get(
                "order_date",
                "",
            )
        )

        save_shared_order(
            student_id,
            order_number,
            order_date,
        )

        return None

    # ========================================================
    # GET:
    # ПОДСТАВЛЯЕМ СОХРАНЕННЫЕ
    # РЕКВИЗИТЫ В ОБЕ ФОРМЫ
    # ========================================================

    def set_input_value(
        source: str,
        field_name: str,
        value: str,
    ) -> str:

        escaped_value = (
            html.escape(
                value,
                quote=True,
            )
        )

        pattern = re.compile(
            (
                r"<input\b"
                r"(?=[^>]*\bname\s*=\s*"
                r"[\"']"
                + re.escape(
                    field_name
                )
                + r"[\"'])"
                r"[^>]*>"
            ),
            flags=re.IGNORECASE,
        )

        def replace(match):

            tag = match.group(0)

            value_pattern = re.compile(
                r"\bvalue\s*=\s*"
                r"([\"']).*?\1",
                flags=re.IGNORECASE,
            )

            if value_pattern.search(
                tag
            ):

                return (
                    value_pattern.sub(
                        (
                            'value="'
                            + escaped_value
                            + '"'
                        ),
                        tag,
                        count=1,
                    )
                )

            return (
                tag[:-1]
                + ' value="'
                + escaped_value
                + '">'
            )

        return pattern.sub(
            replace,
            source,
            count=1,
        )


    @app.after_request
    def order_sync_after_request(
        response,
    ):

        if not is_post_document_page():
            return response

        if response.status_code != 200:
            return response

        content_type = (
            response.content_type
            or ""
        )

        if (
            "text/html"
            not in content_type
        ):
            return response

        student_id = (
            current_student_id()
        )

        if not student_id:
            return response

        shared = get_shared_order(
            student_id
        )

        if not shared:
            return response

        try:
            source = response.get_data(
                as_text=True
            )

        except Exception:
            return response

        # Если на странице нет полей
        # приказа — ничего не делаем.
        if (
            'name="order_number"'
            not in source
            and "name='order_number'"
            not in source
        ):
            return response

        source = set_input_value(
            source,
            "order_number",
            shared[
                "order_number"
            ],
        )

        source = set_input_value(
            source,
            "order_date",
            shared[
                "order_date"
            ],
        )

        response.set_data(
            source
        )

        # Длина HTML изменилась.
        response.headers.pop(
            "Content-Length",
            None,
        )

        return response
