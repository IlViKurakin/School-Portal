
from __future__ import annotations

import html
import inspect
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import (
    abort,
    g,
    request,
    send_file,
    send_from_directory,
    url_for,
)


DOCUMENT_LABELS = {
    "certificate":
        "Справка",

    "order_extract":
        "Выписка из приказа",
}


def apply_post_docs_formats_v9_5_2(
    app,
    namespace: dict[str, Any],
) -> None:

    if app.extensions.get(
        "post_docs_formats_v9_5_2"
    ):
        return

    app.extensions[
        "post_docs_formats_v9_5_2"
    ] = True

    database_path: Path = namespace[
        "DATABASE_PATH"
    ]

    base_dir: Path = namespace[
        "BASE_DIR"
    ]

    generated_dir = (
        base_dir
        / "generated_documents"
    )

    pdf_dir = (
        generated_dir
        / "pdf"
    )

    pdf_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    get_student_or_404 = namespace[
        "get_student_or_404"
    ]

    roles_required = namespace[
        "roles_required"
    ]

    audit = namespace.get(
        "audit"
    )

    # ====================================================
    # База
    # ====================================================

    def connect():

        connection = (
            sqlite3.connect(
                database_path
            )
        )

        connection.row_factory = (
            sqlite3.Row
        )

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection


    def get_document(
        kind: str,
        row_id: int,
    ):

        connection = connect()

        try:

            if kind == "current":

                return connection.execute(
                    """
                    SELECT *
                    FROM
                        post_enrollment_documents

                    WHERE
                        id = ?
                        AND status = 'issued'
                        AND trim(
                            coalesce(
                                stored_name,
                                ''
                            )
                        ) <> ''
                    """,
                    (
                        row_id,
                    ),
                ).fetchone()

            if kind == "history":

                return connection.execute(
                    """
                    SELECT *
                    FROM
                        post_enrollment_document_history

                    WHERE
                        id = ?
                        AND trim(
                            coalesce(
                                stored_name,
                                ''
                            )
                        ) <> ''
                    """,
                    (
                        row_id,
                    ),
                ).fetchone()

            abort(404)

        finally:

            connection.close()

    # ====================================================
    # Имена файлов
    # ====================================================

    def clean_name(
        value,
    ):

        value = re.sub(
            r'[<>:"/\\|?*]+',
            "_",
            str(
                value or ""
            ),
        )

        return (
            value.strip(
                " ."
            )
            or "Документ"
        )


    def download_name(
        row,
        extension,
    ):

        if (
            row["document_type"]
            == "certificate"
        ):

            number = (
                (
                    " № "
                    + str(
                        row[
                            "certificate_number"
                        ]
                    )
                )
                if row[
                    "certificate_number"
                ]
                is not None

                else ""
            )

            name = (
                "Справка"
                + number
            )

        else:

            name = (
                "Выписка из приказа"
            )

        if row["issue_date"]:

            try:

                date_text = (
                    datetime.strptime(
                        row[
                            "issue_date"
                        ],
                        "%Y-%m-%d",
                    )
                    .strftime(
                        "%d.%m.%Y"
                    )
                )

                name += (
                    " от "
                    + date_text
                )

            except ValueError:

                pass

        return (
            clean_name(
                name
            )
            + extension
        )

    # ====================================================
    # PDF
    #
    # Сначала пробуем Microsoft Word.
    # Если его нет — LibreOffice.
    # ====================================================

    def powershell_quote(
        value,
    ):

        return (
            "'"
            + str(
                value
            ).replace(
                "'",
                "''",
            )
            + "'"
        )


    def word_to_pdf(
        source: Path,
        target: Path,
    ):

        if os.name != "nt":

            return (
                False,
                (
                    "Microsoft Word COM "
                    "доступен только Windows."
                ),
            )

        powershell = (
            shutil.which(
                "powershell.exe"
            )
            or
            shutil.which(
                "powershell"
            )
        )

        if not powershell:

            return (
                False,
                "PowerShell не найден.",
            )

        source_value = (
            powershell_quote(
                source.resolve()
            )
        )

        target_value = (
            powershell_quote(
                target.resolve()
            )
        )

        command = f"""
$ErrorActionPreference = 'Stop'
$word = $null
$doc = $null

try {{

    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0

    $doc = $word.Documents.Open(
        {source_value},
        $false,
        $true
    )

    $doc.ExportAsFixedFormat(
        {target_value},
        17
    )

}}
finally {{

    if ($doc -ne $null) {{
        $doc.Close(0)
    }}

    if ($word -ne $null) {{
        $word.Quit()
    }}
}}
"""

        try:

            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],

                capture_output=True,
                text=True,
                timeout=120,

                creationflags=getattr(
                    subprocess,
                    "CREATE_NO_WINDOW",
                    0,
                ),
            )

        except Exception as error:

            return (
                False,
                str(
                    error
                ),
            )

        if (
            result.returncode == 0
            and target.exists()
        ):

            return (
                True,
                "",
            )

        message = (
            result.stderr.strip()
            or
            result.stdout.strip()
            or
            (
                "Word вернул код "
                f"{result.returncode}"
            )
        )

        return (
            False,
            message,
        )


    def libreoffice_to_pdf(
        source: Path,
        target: Path,
    ):

        executable = (
            shutil.which(
                "soffice"
            )
            or
            shutil.which(
                "libreoffice"
            )
        )

        if not executable:

            return (
                False,
                "LibreOffice не найден.",
            )

        with tempfile.TemporaryDirectory() \
                as directory:

            directory = Path(
                directory
            )

            try:

                result = subprocess.run(
                    [
                        executable,
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        str(
                            directory
                        ),
                        str(
                            source
                        ),
                    ],

                    capture_output=True,
                    text=True,
                    timeout=120,

                    creationflags=getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    ),
                )

            except Exception as error:

                return (
                    False,
                    str(
                        error
                    ),
                )

            produced = (
                directory
                / (
                    source.stem
                    + ".pdf"
                )
            )

            if (
                result.returncode == 0
                and produced.exists()
            ):

                shutil.copyfile(
                    produced,
                    target,
                )

                return (
                    True,
                    "",
                )

            return (
                False,
                (
                    result.stderr.strip()
                    or
                    result.stdout.strip()
                    or
                    "Ошибка LibreOffice."
                ),
            )


    def get_pdf(
        source: Path,
    ):

        if not source.exists():

            abort(
                404,
                (
                    "Сформированный "
                    "Word-файл не найден."
                ),
            )

        target = (
            pdf_dir
            / (
                source.stem
                + ".pdf"
            )
        )

        # DOCX после финального
        # формирования неизменяемый,
        # поэтому PDF можно кэшировать.
        if (
            target.exists()
            and
            target.stat().st_mtime
            >=
            source.stat().st_mtime
        ):

            return target

        target.unlink(
            missing_ok=True
        )

        errors = []

        success, message = (
            word_to_pdf(
                source,
                target,
            )
        )

        if success:

            return target

        if message:

            errors.append(
                (
                    "Microsoft Word: "
                    + message
                )
            )

        success, message = (
            libreoffice_to_pdf(
                source,
                target,
            )
        )

        if success:

            return target

        if message:

            errors.append(
                (
                    "LibreOffice: "
                    + message
                )
            )

        abort(
            500,
            (
                "Не удалось сформировать PDF. "
                "На компьютере или сервере "
                "должен быть установлен "
                "Microsoft Word или LibreOffice. "
                + " | ".join(
                    errors
                )
            ),
        )

    # ====================================================
    # 1. МУП — повторное формирование
    # ====================================================

    repeat_endpoint = (
        "post_document_generate_again_v9_5"
    )

    if (
        repeat_endpoint
        in app.view_functions
    ):

        old_view = (
            app.view_functions[
                repeat_endpoint
            ]
        )

        # Снимаем старый
        # roles_required(
        #   attestation, admin
        # )
        original_view = (
            inspect.unwrap(
                old_view
            )
        )

        # И назначаем новый доступ.
        app.view_functions[
            repeat_endpoint
        ] = roles_required(
            "branch",
            "attestation",
            "admin",
        )(
            original_view
        )

        print(
            (
                "Повторное формирование: "
                "МУП доступ открыт."
            )
        )

    else:

        print(
            (
                "ВНИМАНИЕ: маршрут "
                "повторного формирования "
                "v9.5 не найден."
            )
        )

    # ====================================================
    # 2. Защита старых Word-ссылок
    #
    # Даже если старая кнопка осталась,
    # branch DOCX получить не сможет.
    # ====================================================

    old_download_rules = []

    for rule in list(
        app.url_map.iter_rules()
    ):

        endpoint = (
            rule.endpoint
        )

        view = (
            app.view_functions.get(
                endpoint
            )
        )

        if not view:

            continue

        try:

            original = (
                inspect.unwrap(
                    view
                )
            )

        except Exception:

            original = view

        module_name = (
            getattr(
                original,
                "__module__",
                "",
            )
            .lower()
        )

        function_name = (
            getattr(
                original,
                "__name__",
                "",
            )
            .lower()
        )

        route_name = (
            rule.rule.lower()
        )

        endpoint_name = (
            endpoint.lower()
        )

        is_post_doc = (
            (
                "portal_post_enrollment"
                in module_name
            )
            or
            (
                "portal_post_docs_history"
                in module_name
            )
            or
            (
                "post-doc"
                in route_name
            )
            or
            (
                "post_document"
                in endpoint_name
            )
        )

        is_download = (
            (
                "download"
                in route_name
            )
            or
            (
                "download"
                in endpoint_name
            )
            or
            (
                "download"
                in function_name
            )
        )

        if not (
            is_post_doc
            and is_download
        ):

            continue

        old_download_rules.append(
            rule.rule
        )

        old_view = view

        def protected_word_download(
            *args,
            __old=old_view,
            **kwargs,
        ):

            if (
                g.current_user
                and
                g.current_user[
                    "role"
                ]
                == "branch"
            ):

                abort(
                    403,
                    (
                        "Для МУП "
                        "сформированные "
                        "документы доступны "
                        "только в PDF."
                    ),
                )

            return __old(
                *args,
                **kwargs,
            )

        app.view_functions[
            endpoint
        ] = (
            protected_word_download
        )

    # ====================================================
    # 3. Новый маршрут Word/PDF
    # ====================================================

    def download_file(
        kind,
        row_id,
        file_format,
    ):

        row = get_document(
            kind,
            row_id,
        )

        if not row:

            abort(
                404
            )

        # Эта функция уже проверяет,
        # что МУП относится
        # к филиалу ученика.
        get_student_or_404(
            row[
                "student_id"
            ]
        )

        role = (
            g.current_user[
                "role"
            ]
        )

        file_format = (
            str(
                file_format
            )
            .lower()
            .strip()
        )

        if file_format not in (
            "pdf",
            "docx",
        ):

            abort(
                404
            )

        # Главное ограничение:
        # Word только аттестация/admin.
        if (
            file_format
            == "docx"
            and role
            == "branch"
        ):

            abort(
                403,
                (
                    "МУП доступен "
                    "только PDF."
                ),
            )

        stored_name = (
            str(
                row[
                    "stored_name"
                ]
                or ""
            )
            .strip()
        )

        source = (
            generated_dir
            / stored_name
        )

        if not source.exists():

            abort(
                404,
                (
                    "Файл документа "
                    "не найден."
                ),
            )

        if audit:

            try:

                audit(
                    (
                        "post_document_"
                        + file_format
                        + "_downloaded"
                    ),

                    row[
                        "student_id"
                    ],

                    (
                        f"kind={kind}; "
                        f"row_id={row_id}; "
                        f"type="
                        f"{row['document_type']}"
                    ),
                )

            except Exception:

                pass

        if (
            file_format
            == "docx"
        ):

            return send_from_directory(
                generated_dir,
                stored_name,

                as_attachment=True,

                download_name=(
                    download_name(
                        row,
                        ".docx",
                    )
                ),
            )

        pdf_path = (
            get_pdf(
                source
            )
        )

        return send_file(
            pdf_path,

            as_attachment=True,

            download_name=(
                download_name(
                    row,
                    ".pdf",
                )
            ),

            mimetype=(
                "application/pdf"
            ),
        )


    if (
        "post_document_file_v9_5_2"
        not in app.view_functions
    ):

        app.add_url_rule(
            (
                "/post-doc-file/"
                "<kind>/"
                "<int:row_id>/"
                "<file_format>"
            ),

            endpoint=(
                "post_document_file_v9_5_2"
            ),

            view_func=roles_required(
                "branch",
                "attestation",
                "admin",
            )(
                download_file
            ),

            methods=[
                "GET",
            ],
        )

    # ====================================================
    # 4. Список сформированных файлов
    # конкретного ученика
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


    def student_documents(
        student_id,
    ):

        connection = connect()

        try:

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

        finally:

            connection.close()

        result = []

        seen = set()

        for kind, rows in (
            (
                "history",
                history,
            ),
            (
                "current",
                current,
            ),
        ):

            for row in rows:

                if (
                    row[
                        "stored_name"
                    ]
                    in seen
                ):

                    continue

                seen.add(
                    row[
                        "stored_name"
                    ]
                )

                result.append(
                    {
                        "kind":
                            kind,

                        "id":
                            row[
                                "id"
                            ],

                        "row":
                            row,
                    }
                )

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


    def build_files_block(
        student_id,
    ):

        rows = (
            student_documents(
                student_id
            )
        )

        if not rows:

            return ""

        role = (
            g.current_user[
                "role"
            ]
        )

        html_parts = [
            """
            <div
                class="card space"
                id="post-doc-files-v952"
            >

                <h2 style="margin-top:0">
                    Скачать сформированные документы
                </h2>

                <p class="muted">
                    Для МУП доступен PDF.
                    Для отдела аттестации
                    и администратора —
                    PDF и Word.
                </p>

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
        ]

        for item in rows:

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

            order_value = "—"

            if row[
                "order_number"
            ]:

                order_value = (
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

                    order_value += (
                        " от "
                        + human_date(
                            row[
                                "order_date"
                            ]
                        )
                    )

            formation_date = (
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
                (
                    '<a class="btn '
                    'btn-primary btn-small" '
                    'href="'
                    + html.escape(
                        pdf_url,
                        quote=True,
                    )
                    + '">PDF</a>'
                )
            )

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
                    (
                        ' <a class="btn '
                        'btn-secondary btn-small" '
                        'href="'
                        + html.escape(
                            word_url,
                            quote=True,
                        )
                        + '">Word</a>'
                    )
                )

            html_parts.append(
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
                                order_value
                            )
                        }
                    </td>

                    <td>
                        {
                            html.escape(
                                formation_date
                            )
                        }
                    </td>

                    <td>
                        {buttons}
                    </td>

                </tr>
                """
            )

        html_parts.append(
            """
                    </tbody>
                </table>
                </div>
            </div>
            """
        )

        return "".join(
            html_parts
        )

    # ====================================================
    # 5. У МУП убираем старые Word-ссылки
    # из интерфейса.
    # ====================================================

    def rule_regex(
        rule,
    ):

        expression = (
            re.escape(
                rule
            )
        )

        expression = re.sub(
            r"\\<[^>]+\\>",
            r"[^/?#]+",
            expression,
        )

        return re.compile(
            (
                "^"
                + expression.rstrip(
                    r"\/"
                )
                + "/?$"
            )
        )


    old_patterns = [
        rule_regex(
            rule
        )
        for rule
        in old_download_rules
    ]


    anchor_pattern = re.compile(
        (
            r"<a\b"
            r"(?P<attrs>[^>]*)>"
            r"(?P<body>.*?)"
            r"</a>"
        ),

        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )


    href_pattern = re.compile(
        r"""href\s*=\s*["']([^"']+)["']""",
        flags=re.IGNORECASE,
    )


    def remove_old_links(
        source,
    ):

        def replacement(
            match,
        ):

            href_match = (
                href_pattern.search(
                    match.group(
                        "attrs"
                    )
                )
            )

            if not href_match:

                return match.group(
                    0
                )

            path = (
                urlparse(
                    html.unescape(
                        href_match.group(
                            1
                        )
                    )
                )
                .path
            )

            if any(
                pattern.match(
                    path
                )
                for pattern
                in old_patterns
            ):

                return ""

            return match.group(
                0
            )

        return (
            anchor_pattern.sub(
                replacement,
                source,
            )
        )


    def final_html(
        response,
    ):

        if (
            request.method
            != "GET"
            or response.status_code
            != 200
            or
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

            match = re.search(
                r"/students/(\d+)/",
                request.path,
            )

            if match:

                student_id = (
                    int(
                        match.group(
                            1
                        )
                    )
                )

        if not student_id:

            return response

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

        # У МУП убираем старые
        # ссылки на DOCX.
        if (
            g.current_user[
                "role"
            ]
            == "branch"
        ):

            source = (
                remove_old_links(
                    source
                )
            )

        # Новый блок скачивания —
        # на главной странице
        # документов после зачисления.
        rule = (
            request.url_rule.rule
            .rstrip(
                "/"
            )

            if request.url_rule

            else ""
        )

        if rule == (
            "/students/"
            "<int:student_id>/"
            "post-docs"
        ):

            block = (
                build_files_block(
                    int(
                        student_id
                    )
                )
            )

            if (
                block
                and
                'id="post-doc-files-v952"'
                not in source
            ):

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


    # Flask вызывает after_request
    # в обратном порядке.
    # Ставим нашу функцию первой
    # в списке, чтобы фактически
    # она выполнилась последней —
    # уже после истории v9.5.
    # V9.5.4 EXTRA BLOCK DISABLED
# final_html больше не регистрируется


    print(
        (
            "Post Docs Formats "
            "v9.5.2 подключен."
        )
    )
