
from __future__ import annotations

import html
import re
import sqlite3

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import (
    g,
    request,
    url_for,
)


DOCUMENT_LABELS = {
    "certificate":
        "Справка",

    "order_extract":
        "Выписка из приказа",
}


def apply_post_docs_ui_v9_5_3(
    app,
    namespace: dict[str, Any],
) -> None:

    if app.extensions.get(
        "post_docs_ui_v9_5_3"
    ):
        return

    app.extensions[
        "post_docs_ui_v9_5_3"
    ] = True

    database_path: Path = namespace[
        "DATABASE_PATH"
    ]

    get_student_or_404 = namespace[
        "get_student_or_404"
    ]

    # ====================================================
    # Проверяем, что PDF-маршрут v9.5.2 есть
    # ====================================================

    if (
        "post_document_file_v9_5_2"
        not in app.view_functions
    ):

        raise RuntimeError(
            (
                "Не найден маршрут "
                "post_document_file_v9_5_2. "
                "Сначала должна быть "
                "установлена v9.5.2."
            )
        )

    # ====================================================
    # База
    # ====================================================

    def connect():

        connection = sqlite3.connect(
            database_path
        )

        connection.row_factory = (
            sqlite3.Row
        )

        return connection


    def table_exists(
        connection,
        table,
    ):

        return (
            connection.execute(
                """
                SELECT 1
                FROM sqlite_master

                WHERE
                    type = 'table'
                    AND name = ?
                """,
                (
                    table,
                ),
            ).fetchone()
            is not None
        )


    # ====================================================
    # Даты
    # ====================================================

    def human_date(
        value,
    ):

        if not value:
            return "—"

        value = str(
            value
        )

        try:

            if len(
                value
            ) == 10:

                return (
                    datetime.strptime(
                        value,
                        "%Y-%m-%d",
                    )
                    .strftime(
                        "%d.%m.%Y"
                    )
                )

            return (
                datetime.fromisoformat(
                    value
                )
                .strftime(
                    "%d.%m.%Y %H:%M"
                )
            )

        except Exception:

            return value


    # ====================================================
    # Все сформированные документы
    # ТОЛЬКО этого ученика
    # ====================================================

    def student_documents(
        student_id,
    ):

        connection = connect()

        try:

            result = []

            seen_files = set()

            # --------------------------------------------
            # История
            # --------------------------------------------

            if table_exists(
                connection,
                "post_enrollment_document_history",
            ):

                history = (
                    connection.execute(
                        """
                        SELECT
                            id,
                            student_id,
                            document_type,
                            certificate_number,
                            order_number,
                            order_date,
                            issue_date,
                            stored_name,
                            generated_at

                        FROM
                            post_enrollment_document_history

                        WHERE
                            student_id = ?

                            AND trim(
                                coalesce(
                                    stored_name,
                                    ''
                                )
                            ) <> ''

                        ORDER BY
                            generated_at DESC,
                            id DESC
                        """,
                        (
                            student_id,
                        ),
                    ).fetchall()
                )

                for row in history:

                    stored_name = (
                        row[
                            "stored_name"
                        ]
                    )

                    if (
                        stored_name
                        in seen_files
                    ):
                        continue

                    seen_files.add(
                        stored_name
                    )

                    result.append(
                        {
                            "kind":
                                "history",

                            "id":
                                row[
                                    "id"
                                ],

                            "row":
                                row,
                        }
                    )

            # --------------------------------------------
            # Текущие финальные документы
            # --------------------------------------------

            current = (
                connection.execute(
                    """
                    SELECT
                        id,
                        student_id,
                        document_type,
                        certificate_number,
                        order_number,
                        order_date,
                        issue_date,
                        stored_name,
                        finalized_at
                            AS generated_at

                    FROM
                        post_enrollment_documents

                    WHERE
                        student_id = ?
                        AND status = 'issued'

                        AND trim(
                            coalesce(
                                stored_name,
                                ''
                            )
                        ) <> ''

                    ORDER BY
                        finalized_at DESC,
                        id DESC
                    """,
                    (
                        student_id,
                    ),
                ).fetchall()
            )

            for row in current:

                stored_name = (
                    row[
                        "stored_name"
                    ]
                )

                if (
                    stored_name
                    in seen_files
                ):
                    continue

                seen_files.add(
                    stored_name
                )

                result.append(
                    {
                        "kind":
                            "current",

                        "id":
                            row[
                                "id"
                            ],

                        "row":
                            row,
                    }
                )

        finally:

            connection.close()

        result.sort(
            key=lambda item:
                str(
                    item[
                        "row"
                    ][
                        "generated_at"
                    ]
                    or
                    item[
                        "row"
                    ][
                        "issue_date"
                    ]
                    or ""
                ),

            reverse=True,
        )

        return result


    # ====================================================
    # Удаляем старые кнопки Word/DOCX
    # ====================================================

    anchor_pattern = re.compile(
        r"<a\b(?P<attrs>[^>]*)>"
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


    def remove_old_download_links(
        source,
    ):

        def replace_anchor(
            match,
        ):

            attrs = match.group(
                "attrs"
            )

            body = match.group(
                "body"
            )

            body_text = re.sub(
                r"<[^>]+>",
                " ",
                body,
            )

            body_text = (
                html.unescape(
                    body_text
                )
                .strip()
                .lower()
            )

            href_match = (
                href_pattern.search(
                    attrs
                )
            )

            href = (
                html.unescape(
                    href_match.group(1)
                )
                if href_match

                else ""
            )

            path = (
                urlparse(
                    href
                ).path.lower()

                if href

                else ""
            )

            # НОВЫЕ кнопки v9.5.2
            # никогда не удаляем.
            if (
                "/post-doc-file/"
                in path
            ):

                return match.group(
                    0
                )

            # Старые кнопки Word/DOCX.
            if (
                "docx"
                in body_text
                or
                "word"
                in body_text
            ):

                return ""

            # Старая кнопка истории могла
            # называться просто «Скачать».
            #
            # Если ссылка ведет на download
            # внутри post-docs — тоже убираем.
            if (
                "скачать"
                in body_text
                and
                (
                    "download"
                    in path
                    or
                    "post-doc"
                    in path
                    or
                    "post_doc"
                    in path
                )
            ):

                return ""

            return match.group(
                0
            )

        return anchor_pattern.sub(
            replace_anchor,
            source,
        )


    # На случай, если старая кнопка
    # находилась внутри form.
    form_pattern = re.compile(
        r"<form\b(?P<attrs>[^>]*)>"
        r"(?P<body>.*?)"
        r"</form>",

        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )


    def remove_old_docx_forms(
        source,
    ):

        def replace_form(
            match,
        ):

            body = (
                html.unescape(
                    re.sub(
                        r"<[^>]+>",
                        " ",
                        match.group(
                            "body"
                        ),
                    )
                )
                .lower()
            )

            if (
                "скачать docx"
                in body
                or
                "скачать word"
                in body
            ):

                return ""

            return match.group(
                0
            )

        return form_pattern.sub(
            replace_form,
            source,
        )


    # ====================================================
    # Новый блок скачивания
    # ====================================================

    def build_download_block(
        student_id,
    ):

        documents = (
            student_documents(
                student_id
            )
        )

        if not documents:

            return ""

        role = (
            g.current_user[
                "role"
            ]
        )

        parts = [
            """
            <div
                class="card space"
                id="post-doc-files-v952"
                style="
                    margin-top:22px;
                    border-left:
                        5px solid #f5c400;
                "
            >

                <h2 style="margin-top:0">
                    Сформированные документы
                </h2>
            """
        ]

        if role == "branch":

            parts.append(
                """
                <p class="muted">
                    Для МУП документы
                    доступны только
                    в защищенном формате PDF.
                </p>
                """
            )

        else:

            parts.append(
                """
                <p class="muted">
                    Доступно скачивание
                    в PDF и Word.
                </p>
                """
            )

        parts.append(
            """
                <div
                    style="overflow-x:auto"
                >

                    <table>

                        <thead>

                            <tr>

                                <th>
                                    Документ
                                </th>

                                <th>
                                    № справки
                                </th>

                                <th>
                                    Приказ
                                </th>

                                <th>
                                    Дата формирования
                                </th>

                                <th>
                                    Скачать
                                </th>

                            </tr>

                        </thead>

                        <tbody>
            """
        )

        for item in documents:

            row = item[
                "row"
            ]

            label = (
                DOCUMENT_LABELS.get(
                    row[
                        "document_type"
                    ],
                    row[
                        "document_type"
                    ],
                )
            )

            certificate_number = (
                str(
                    row[
                        "certificate_number"
                    ]
                )

                if row[
                    "certificate_number"
                ]
                is not None

                else "—"
            )

            order_text = "—"

            if row[
                "order_number"
            ]:

                order_text = (
                    "№ "
                    + str(
                        row[
                            "order_number"
                        ]
                    )
                )

                if row[
                    "order_date"
                ]:

                    order_text += (
                        " от "
                        + human_date(
                            row[
                                "order_date"
                            ]
                        )
                    )

            date_text = (
                human_date(
                    row[
                        "issue_date"
                    ]
                    or
                    row[
                        "generated_at"
                    ]
                )
            )

            pdf_url = url_for(
                "post_document_file_v9_5_2",

                kind=
                    item[
                        "kind"
                    ],

                row_id=
                    item[
                        "id"
                    ],

                file_format=
                    "pdf",
            )

            buttons = (
                '<a '
                'class="btn btn-primary btn-small" '
                'href="'
                + html.escape(
                    pdf_url,
                    quote=True,
                )
                + '">'
                'Скачать PDF'
                '</a>'
            )

            # Word только
            # аттестация и администратор.
            if role in (
                "attestation",
                "admin",
            ):

                word_url = url_for(
                    "post_document_file_v9_5_2",

                    kind=
                        item[
                            "kind"
                        ],

                    row_id=
                        item[
                            "id"
                        ],

                    file_format=
                        "docx",
                )

                buttons += (
                    ' '
                    '<a '
                    'class="btn btn-secondary btn-small" '
                    'href="'
                    + html.escape(
                        word_url,
                        quote=True,
                    )
                    + '">'
                    'Скачать Word'
                    '</a>'
                )

            parts.append(
                f"""
                <tr>

                    <td>
                        <strong>
                            {
                                html.escape(
                                    str(
                                        label
                                    )
                                )
                            }
                        </strong>
                    </td>

                    <td>
                        {
                            html.escape(
                                certificate_number
                            )
                        }
                    </td>

                    <td>
                        {
                            html.escape(
                                order_text
                            )
                        }
                    </td>

                    <td>
                        {
                            html.escape(
                                date_text
                            )
                        }
                    </td>

                    <td>
                        {buttons}
                    </td>

                </tr>
                """
            )

        parts.append(
            """
                        </tbody>

                    </table>

                </div>

            </div>
            """
        )

        return "".join(
            parts
        )


    # ====================================================
    # Оборачиваем именно основную страницу
    # «Документы после зачисления»
    # ====================================================

    post_docs_endpoint = None

    for rule in (
        app.url_map.iter_rules()
    ):

        if (
            rule.rule.rstrip("/")
            ==
            (
                "/students/"
                "<int:student_id>/"
                "post-docs"
            )
        ):

            post_docs_endpoint = (
                rule.endpoint
            )

            break


    if not post_docs_endpoint:

        raise RuntimeError(
            (
                "Не найден основной маршрут "
                "«Документы после зачисления»."
            )
        )


    original_post_docs_view = (
        app.view_functions[
            post_docs_endpoint
        ]
    )


    def post_docs_view_v953(
        *args,
        **kwargs,
    ):

        result = (
            original_post_docs_view(
                *args,
                **kwargs,
            )
        )

        response = (
            app.make_response(
                result
            )
        )

        if (
            request.method
            != "GET"
            or
            response.status_code
            != 200
            or
            "text/html"
            not in (
                response.content_type
                or ""
            )
        ):

            return response

        student_id = (
            kwargs.get(
                "student_id"
            )
        )

        if not student_id:

            student_id = (
                (
                    request.view_args
                    or {}
                )
                .get(
                    "student_id"
                )
            )

        if not student_id:

            return response

        # Серверная проверка права
        # на конкретного ученика.
        get_student_or_404(
            int(
                student_id
            )
        )

        source = (
            response.get_data(
                as_text=True
            )
        )

        # --------------------------------
        # Удаляем старые download-кнопки.
        # --------------------------------

        source = (
            remove_old_docx_forms(
                source
            )
        )

        source = (
            remove_old_download_links(
                source
            )
        )

        # --------------------------------
        # Если старый v9.5.2 уже успел
        # вставить свой блок —
        # удаляем его целиком.
        # --------------------------------

        source = re.sub(
            (
                r'<div\b'
                r'(?=[^>]*'
                r'id=["\']'
                r'post-doc-files-v952'
                r'["\'])'
                r'[^>]*>'
                r'.*?'
                r'</div>'
            ),

            "",

            source,

            count=1,

            flags=(
                re.IGNORECASE
                | re.DOTALL
            ),
        )

        # --------------------------------
        # Вставляем правильный блок.
        # --------------------------------

        block = (
            build_download_block(
                int(
                    student_id
                )
            )
        )

        if block:

            # Ставим ближе к концу
            # содержательной части.
            if (
                "</section>"
                in source
            ):

                source = (
                    source.replace(
                        "</section>",

                        (
                            block
                            + "</section>"
                        ),

                        1,
                    )
                )

            elif (
                "</main>"
                in source
            ):

                source = (
                    source.replace(
                        "</main>",

                        (
                            block
                            + "</main>"
                        ),

                        1,
                    )
                )

            else:

                source += block

        response.set_data(
            source
        )

        response.headers.pop(
            "Content-Length",
            None,
        )

        return response


    app.view_functions[
        post_docs_endpoint
    ] = (
        post_docs_view_v953
    )


    print(
        (
            "Post Docs UI v9.5.3 "
            "подключен."
        )
    )
