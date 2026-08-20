
from __future__ import annotations

import html
import re
import sqlite3

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import (
    g,
    request,
    url_for,
)


def apply_post_docs_buttons_v9_5_4(
    app,
    namespace: dict[str, Any],
) -> None:

    if app.extensions.get(
        "post_docs_buttons_v9_5_4"
    ):
        return

    app.extensions[
        "post_docs_buttons_v9_5_4"
    ] = True

    database_path: Path = namespace[
        "DATABASE_PATH"
    ]

    get_student_or_404 = namespace[
        "get_student_or_404"
    ]

    # Новый маршрут PDF/Word
    # уже создан модулем v9.5.2.
    if (
        "post_document_file_v9_5_2"
        not in app.view_functions
    ):

        raise RuntimeError(
            "Не найден маршрут "
            "post_document_file_v9_5_2."
        )

    # ========================================================
    # База
    # ========================================================

    def connect():

        connection = sqlite3.connect(
            database_path
        )

        connection.row_factory = (
            sqlite3.Row
        )

        return connection


    def current_document(
        document_id,
    ):

        connection = connect()

        try:

            return connection.execute(
                """
                SELECT *
                FROM post_enrollment_documents
                WHERE id = ?
                """,
                (
                    document_id,
                ),
            ).fetchone()

        finally:

            connection.close()


    def current_document_by_student(
        student_id,
        document_type,
    ):

        connection = connect()

        try:

            return connection.execute(
                """
                SELECT *
                FROM post_enrollment_documents

                WHERE
                    student_id = ?
                    AND document_type = ?

                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    student_id,
                    document_type,
                ),
            ).fetchone()

        finally:

            connection.close()


    def history_document(
        history_id,
    ):

        connection = connect()

        try:

            return connection.execute(
                """
                SELECT *
                FROM
                    post_enrollment_document_history

                WHERE id = ?
                """,
                (
                    history_id,
                ),
            ).fetchone()

        finally:

            connection.close()

    # ========================================================
    # По старой ссылке определяем,
    # какой документ она скачивала.
    # ========================================================

    adapter = app.url_map.bind(
        "localhost"
    )


    def resolve_old_link(
        href,
    ):

        if not href:
            return None

        parsed = urlparse(
            html.unescape(
                href
            )
        )

        path = parsed.path

        # Уже новые кнопки.
        if (
            "/post-doc-file/"
            in path
        ):
            return None

        try:

            endpoint, values = (
                adapter.match(
                    path,
                    method="GET",
                )
            )

        except Exception:

            return None

        # --------------------------------
        # История v9.5
        # --------------------------------

        history_id = (
            values.get(
                "history_id"
            )
        )

        if history_id:

            row = history_document(
                int(
                    history_id
                )
            )

            if row:

                return {
                    "kind":
                        "history",

                    "id":
                        row[
                            "id"
                        ],

                    "student_id":
                        row[
                            "student_id"
                        ],
                }

        # --------------------------------
        # Текущий документ:
        # возможный document_id
        # --------------------------------

        for key in (
            "document_id",
            "post_document_id",
            "post_doc_id",
        ):

            value = values.get(
                key
            )

            if value is None:
                continue

            try:

                row = (
                    current_document(
                        int(
                            value
                        )
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                row = None

            if row:

                return {
                    "kind":
                        "current",

                    "id":
                        row[
                            "id"
                        ],

                    "student_id":
                        row[
                            "student_id"
                        ],
                }

        # --------------------------------
        # v8 может использовать:
        #
        # student_id +
        # document_type
        # --------------------------------

        student_id = values.get(
            "student_id"
        )

        document_type = values.get(
            "document_type"
        )

        if (
            student_id is not None
            and document_type
            in (
                "certificate",
                "order_extract",
            )
        ):

            row = (
                current_document_by_student(
                    int(
                        student_id
                    ),
                    document_type,
                )
            )

            if row:

                return {
                    "kind":
                        "current",

                    "id":
                        row[
                            "id"
                        ],

                    "student_id":
                        row[
                            "student_id"
                        ],
                }

        # --------------------------------
        # Запасной вариант:
        # единственный числовой аргумент.
        # --------------------------------

        numeric_values = []

        for value in values.values():

            try:

                numeric_values.append(
                    int(
                        value
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                pass

        if len(
            numeric_values
        ) == 1:

            possible_id = (
                numeric_values[0]
            )

            row = current_document(
                possible_id
            )

            if row:

                return {
                    "kind":
                        "current",

                    "id":
                        row[
                            "id"
                        ],

                    "student_id":
                        row[
                            "student_id"
                        ],
                }

            row = history_document(
                possible_id
            )

            if row:

                return {
                    "kind":
                        "history",

                    "id":
                        row[
                            "id"
                        ],

                    "student_id":
                        row[
                            "student_id"
                        ],
                }

        return None

    # ========================================================
    # Формируем кнопки
    # ========================================================

    def download_buttons(
        document,
        original_class="btn btn-primary",
    ):

        role = (
            g.current_user[
                "role"
            ]
        )

        pdf_url = url_for(
            "post_document_file_v9_5_2",

            kind=
                document[
                    "kind"
                ],

            row_id=
                document[
                    "id"
                ],

            file_format=
                "pdf",
        )

        # МУП — только PDF.
        if role == "branch":

            return (
                '<a class="'
                + html.escape(
                    original_class,
                    quote=True,
                )
                + '" href="'
                + html.escape(
                    pdf_url,
                    quote=True,
                )
                + '">'
                + 'Скачать PDF'
                + '</a>'
            )

        # Аттестация / администратор:
        # Word + PDF.

        docx_url = url_for(
            "post_document_file_v9_5_2",

            kind=
                document[
                    "kind"
                ],

            row_id=
                document[
                    "id"
                ],

            file_format=
                "docx",
        )

        return (
            '<a class="'
            + html.escape(
                original_class,
                quote=True,
            )
            + '" href="'
            + html.escape(
                docx_url,
                quote=True,
            )
            + '">'
            + 'Скачать DOCX'
            + '</a>'

            + ' '

            + '<a class="'
            + html.escape(
                original_class,
                quote=True,
            )
            + '" href="'
            + html.escape(
                pdf_url,
                quote=True,
            )
            + '">'
            + 'Скачать PDF'
            + '</a>'
        )

    # ========================================================
    # Заменяем ТОЛЬКО существующие
    # кнопки скачивания.
    #
    # Никаких новых блоков.
    # ========================================================

    anchor_pattern = re.compile(
        r"<a\b"
        r"(?P<attrs>[^>]*)>"
        r"(?P<body>.*?)"
        r"</a>",

        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )


    href_pattern = re.compile(
        r"""href\s*=\s*["']([^"']+)["']""",
        flags=re.IGNORECASE,
    )


    class_pattern = re.compile(
        r"""class\s*=\s*["']([^"']*)["']""",
        flags=re.IGNORECASE,
    )


    def replace_download_anchor(
        match,
    ):

        attrs = match.group(
            "attrs"
        )

        body = match.group(
            "body"
        )

        visible_text = (
            html.unescape(
                re.sub(
                    r"<[^>]+>",
                    " ",
                    body,
                )
            )
            .strip()
            .lower()
        )

        # Не трогаем никакие другие
        # ссылки страницы.
        if (
            "скачать"
            not in visible_text
            and
            "docx"
            not in visible_text
            and
            "word"
            not in visible_text
        ):

            return match.group(
                0
            )

        href_match = (
            href_pattern.search(
                attrs
            )
        )

        if not href_match:

            return match.group(
                0
            )

        href = (
            href_match.group(
                1
            )
        )

        # Уже исправленная ссылка.
        if (
            "/post-doc-file/"
            in href
        ):

            return match.group(
                0
            )

        document = (
            resolve_old_link(
                href
            )
        )

        if not document:

            return match.group(
                0
            )

        # Дополнительная серверная
        # проверка филиала.
        try:

            get_student_or_404(
                document[
                    "student_id"
                ]
            )

        except Exception:

            return match.group(
                0
            )

        class_match = (
            class_pattern.search(
                attrs
            )
        )

        css_class = (
            class_match.group(
                1
            )

            if class_match

            else
            "btn btn-primary"
        )

        return download_buttons(
            document,
            css_class,
        )

    # ========================================================
    # POST-DOC страницы
    # ========================================================

    def fix_post_docs_html(
        response,
    ):

        if (
            request.method
            != "GET"
        ):

            return response

        if (
            response.status_code
            != 200
        ):

            return response

        if (
            "text/html"
            not in (
                response.content_type
                or ""
            )
        ):

            return response

        if (
            "/post-docs"
            not in request.path
        ):

            return response

        source = (
            response.get_data(
                as_text=True
            )
        )

        # Здесь только замена кнопок.
        #
        # Новый HTML-блок
        # НЕ добавляется.

        source = (
            anchor_pattern.sub(
                replace_download_anchor,
                source,
            )
        )

        response.set_data(
            source
        )

        response.headers.pop(
            "Content-Length",
            None,
        )

        return response

    # Flask запускает after_request
    # в обратном порядке.
    #
    # Добавляем в начало списка,
    # чтобы замена кнопок выполнилась
    # ПОСЛЕ того, как v9.5 добавит
    # историю документов.

    app.after_request_funcs.setdefault(
        None,
        [],
    ).insert(
        0,
        fix_post_docs_html,
    )

    print(
        "Post Docs Buttons "
        "v9.5.4 подключен."
    )
