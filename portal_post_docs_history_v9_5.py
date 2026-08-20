
from __future__ import annotations

import html
import re
import sqlite3

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    abort,
    flash,
    g,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)


DOCUMENT_LABELS = {
    "order_extract":
        "Выписка из приказа",

    "certificate":
        "Справка",
}


def apply_post_docs_history_v9_5(
    app,
    namespace: dict[str, Any],
) -> None:

    if app.extensions.get(
        "post_docs_history_v9_5"
    ):
        return

    app.extensions[
        "post_docs_history_v9_5"
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

    get_student_or_404 = namespace[
        "get_student_or_404"
    ]

    roles_required = namespace[
        "roles_required"
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
        name: str,
    ) -> bool:

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
                    name,
                ),
            ).fetchone()
            is not None
        )


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
                post_enrollment_document_history
                (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    student_id INTEGER
                        NOT NULL,

                    source_post_document_id
                        INTEGER,

                    document_type TEXT
                        NOT NULL,

                    certificate_number
                        INTEGER,

                    order_number TEXT,
                    order_date TEXT,

                    issue_date TEXT,
                    academic_year TEXT,

                    snapshot_json TEXT,

                    stored_name TEXT
                        NOT NULL,

                    generated_by INTEGER,

                    generated_at TEXT
                        NOT NULL,

                    FOREIGN KEY(student_id)
                        REFERENCES students(id),

                    FOREIGN KEY(generated_by)
                        REFERENCES users(id)
                )
                """
            )

            connection.execute(
                """
                CREATE UNIQUE INDEX
                IF NOT EXISTS
                idx_post_history_unique_file

                ON
                    post_enrollment_document_history
                    (
                        source_post_document_id,
                        stored_name,
                        generated_at
                    )
                """
            )

            connection.execute(
                """
                CREATE INDEX
                IF NOT EXISTS
                idx_post_history_student

                ON
                    post_enrollment_document_history
                    (
                        student_id,
                        generated_at
                    )
                """
            )

            connection.commit()

            # Уже существующие
            # сформированные документы
            # сразу переносим в историю.
            issued_rows = (
                connection.execute(
                    """
                    SELECT *
                    FROM
                        post_enrollment_documents

                    WHERE
                        status = 'issued'

                        AND
                        trim(
                            coalesce(
                                stored_name,
                                ''
                            )
                        ) <> ''
                    """
                ).fetchall()
            )

            for row in issued_rows:
                archive_row(
                    connection,
                    row,
                )

            connection.commit()

        finally:
            connection.close()


    def archive_row(
        connection,
        row,
    ) -> None:

        stored_name = (
            row["stored_name"]
            or ""
        ).strip()

        if not stored_name:
            return

        generated_at = (
            row["finalized_at"]
            or row["updated_at"]
            or row["created_at"]
            or datetime.now().isoformat(
                timespec="seconds"
            )
        )

        generated_by = (
            row["finalized_by"]
            or row["updated_by"]
            or row["created_by"]
        )

        exists = connection.execute(
            """
            SELECT id
            FROM
                post_enrollment_document_history

            WHERE
                source_post_document_id = ?
                AND stored_name = ?
                AND generated_at = ?

            LIMIT 1
            """,
            (
                row["id"],
                stored_name,
                generated_at,
            ),
        ).fetchone()

        if exists:
            return

        connection.execute(
            """
            INSERT INTO
                post_enrollment_document_history
            (
                student_id,
                source_post_document_id,
                document_type,
                certificate_number,
                order_number,
                order_date,
                issue_date,
                academic_year,
                snapshot_json,
                stored_name,
                generated_by,
                generated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["student_id"],
                row["id"],
                row["document_type"],
                row["certificate_number"],
                row["order_number"],
                row["order_date"],
                row["issue_date"],
                row["academic_year"],
                row["snapshot_json"],
                stored_name,
                generated_by,
                generated_at,
            ),
        )


    migrate()

    # ========================================================
    # СОХРАНЯЕМ КАЖДОЕ НОВОЕ ФОРМИРОВАНИЕ
    # В ИСТОРИЮ
    # ========================================================

    def archive_issued_for_student(
        student_id: int,
    ):

        connection = connect()

        try:

            rows = connection.execute(
                """
                SELECT *
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
                """,
                (
                    student_id,
                ),
            ).fetchall()

            for row in rows:
                archive_row(
                    connection,
                    row,
                )

            connection.commit()

        finally:
            connection.close()


    def student_id_from_request():

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

        match = re.search(
            r"/students/(\d+)/",
            request.path,
        )

        if match:
            return int(
                match.group(1)
            )

        return None


    @app.after_request
    def archive_after_formation(
        response,
    ):

        # После любого действия
        # в разделе post-docs проверяем,
        # не появился ли новый issued.
        if (
            "/post-docs"
            not in request.path
        ):
            return response

        student_id = (
            student_id_from_request()
        )

        if not student_id:
            return response

        try:
            archive_issued_for_student(
                student_id
            )

        except Exception as error:
            print(
                "V9.5 history archive:",
                repr(error),
            )

        return response

    # ========================================================
    # СФОРМИРОВАТЬ ЕЩЕ РАЗ
    # ========================================================

    def generate_again(
        student_id: int,
        document_type: str,
    ):

        student = get_student_or_404(
            student_id
        )

        if document_type not in (
            "order_extract",
            "certificate",
        ):
            abort(404)

        connection = connect()

        try:

            row = connection.execute(
                """
                SELECT *
                FROM
                    post_enrollment_documents

                WHERE
                    student_id = ?
                    AND document_type = ?

                LIMIT 1
                """,
                (
                    student_id,
                    document_type,
                ),
            ).fetchone()

            if not row:

                flash(
                    (
                        "Документ еще ни разу "
                        "не формировался."
                    ),
                    "info",
                )

                return redirect(
                    url_for(
                        find_post_docs_endpoint(),
                        student_id=student_id,
                    )
                )

            # Перед сбросом обязательно
            # сохраняем текущий финальный
            # файл в историю.
            if (
                row["status"]
                == "issued"
                and row["stored_name"]
            ):
                archive_row(
                    connection,
                    row,
                )

            timestamp = (
                datetime.now()
                .isoformat(
                    timespec="seconds"
                )
            )

            # Реквизиты приказа НЕ очищаем.
            #
            # Благодаря v9.4 они останутся
            # общими для справки и выписки.
            #
            # Новый финальный документ
            # получит новую дату формирования.
            if document_type == "certificate":

                # Для новой справки
                # обязательно нужен
                # новый уникальный номер.
                connection.execute(
                    """
                    UPDATE
                        post_enrollment_documents

                    SET
                        status = 'draft',

                        certificate_number = NULL,

                        issue_date = NULL,
                        snapshot_json = NULL,
                        stored_name = NULL,

                        name_override = NULL,

                        finalized_by = NULL,
                        finalized_at = NULL,

                        updated_by = ?,
                        updated_at = ?

                    WHERE id = ?
                    """,
                    (
                        g.current_user["id"],
                        timestamp,
                        row["id"],
                    ),
                )

            else:

                connection.execute(
                    """
                    UPDATE
                        post_enrollment_documents

                    SET
                        status = 'draft',

                        issue_date = NULL,
                        snapshot_json = NULL,
                        stored_name = NULL,

                        name_override = NULL,

                        finalized_by = NULL,
                        finalized_at = NULL,

                        updated_by = ?,
                        updated_at = ?

                    WHERE id = ?
                    """,
                    (
                        g.current_user["id"],
                        timestamp,
                        row["id"],
                    ),
                )

            connection.commit()

        finally:
            connection.close()

        if audit:

            try:
                audit(
                    "post_document_reopened",
                    student_id,
                    (
                        "Повторное формирование: "
                        + DOCUMENT_LABELS[
                            document_type
                        ]
                    ),
                )

            except Exception:
                pass

        flash(
            (
                DOCUMENT_LABELS[
                    document_type
                ]
                + " открыта для нового "
                "формирования. "
                "Дата нового документа "
                "будет установлена "
                "при финальном сохранении."
            ),
            "success",
        )

        return redirect(
            url_for(
                find_post_docs_endpoint(),
                student_id=student_id,
            )
        )

    # ========================================================
    # ИЩЕМ ОСНОВНОЙ ENDPOINT V8
    # ДИНАМИЧЕСКИ
    # ========================================================

    def find_post_docs_endpoint():

        for rule in app.url_map.iter_rules():

            normalized = (
                rule.rule.rstrip("/")
            )

            if normalized == (
                "/students/"
                "<int:student_id>/"
                "post-docs"
            ):
                return rule.endpoint

        raise RuntimeError(
            "Не найден основной маршрут "
            "/students/<student_id>/post-docs"
        )

    # ========================================================
    # СКАЧИВАНИЕ ДОКУМЕНТА ИЗ ИСТОРИИ
    # ========================================================

    def download_history_document(
        history_id: int,
    ):

        connection = connect()

        try:

            row = connection.execute(
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

        if not row:
            abort(404)

        # Проверяем доступ к ученику.
        get_student_or_404(
            row["student_id"]
        )

        path = (
            generated_dir
            / row["stored_name"]
        )

        if not path.exists():
            abort(
                404,
                "Файл сформированного "
                "документа не найден."
            )

        document_name = (
            DOCUMENT_LABELS.get(
                row["document_type"],
                "Документ",
            )
        )

        if (
            row["document_type"]
            == "certificate"
            and row["certificate_number"]
        ):
            download_name = (
                f"Справка №"
                f"{row['certificate_number']}.docx"
            )

        else:
            date_part = (
                row["issue_date"]
                or ""
            )

            download_name = (
                f"{document_name} "
                f"{date_part}.docx"
            ).strip()

        return send_from_directory(
            generated_dir,
            row["stored_name"],
            as_attachment=True,
            download_name=download_name,
        )

    # ========================================================
    # HTML ИСТОРИИ КОНКРЕТНОГО УЧЕНИКА
    # ========================================================

    def human_date(value):

        if not value:
            return "—"

        try:

            # YYYY-MM-DD
            if len(value) == 10:
                return datetime.strptime(
                    value,
                    "%Y-%m-%d",
                ).strftime(
                    "%d.%m.%Y"
                )

            # ISO datetime
            return datetime.fromisoformat(
                value
            ).strftime(
                "%d.%m.%Y %H:%M"
            )

        except Exception:
            return str(value)


    def build_student_history_html(
        student_id: int,
    ) -> str:

        connection = connect()

        try:

            history = connection.execute(
                """
                SELECT
                    h.*,
                    u.full_name
                        AS generated_by_name

                FROM
                    post_enrollment_document_history
                    h

                LEFT JOIN users u
                    ON u.id =
                       h.generated_by

                WHERE
                    h.student_id = ?

                ORDER BY
                    h.generated_at DESC,
                    h.id DESC
                """,
                (
                    student_id,
                ),
            ).fetchall()

            current_docs = (
                connection.execute(
                    """
                    SELECT *
                    FROM
                        post_enrollment_documents

                    WHERE student_id = ?
                    """,
                    (
                        student_id,
                    ),
                ).fetchall()
            )

        finally:
            connection.close()

        csrf = html.escape(
            session.get(
                "csrf_token",
                "",
            ),
            quote=True,
        )

        parts = []

        # --------------------------------
        # Повторное формирование
        # --------------------------------

        issued = {
            row["document_type"]: row
            for row in current_docs
            if row["status"] == "issued"
        }

        if issued:

            parts.append(
                """
                <div
                    class="card space"
                    style="
                        border-left:
                            5px solid #f5c400;
                    "
                >
                    <h2 style="margin-top:0">
                        Сформировать документ еще раз
                    </h2>

                    <p class="muted">
                        Предыдущий документ
                        останется в истории.
                        Новый документ получит
                        актуальную дату формирования.
                    </p>

                    <div
                        style="
                            display:flex;
                            gap:10px;
                            flex-wrap:wrap;
                        "
                    >
                """
            )

            for document_type in (
                "certificate",
                "order_extract",
            ):

                if (
                    document_type
                    not in issued
                ):
                    continue

                label = html.escape(
                    DOCUMENT_LABELS[
                        document_type
                    ]
                )

                action = url_for(
                    "post_document_generate_again_v9_5",
                    student_id=student_id,
                    document_type=document_type,
                )

                parts.append(
                    f"""
                    <form
                        method="post"
                        action="{html.escape(
                            action,
                            quote=True
                        )}"
                        class="inline"
                    >
                        <input
                            type="hidden"
                            name="csrf_token"
                            value="{csrf}"
                        >

                        <button
                            class="btn btn-primary"
                            type="submit"
                        >
                            Сформировать еще раз:
                            {label}
                        </button>
                    </form>
                    """
                )

            parts.append(
                """
                    </div>
                </div>
                """
            )

        # --------------------------------
        # История только этого ученика
        # --------------------------------

        parts.append(
            """
            <div class="space">
                <h2>
                    История сформированных документов
                </h2>
            """
        )

        if not history:

            parts.append(
                """
                <div class="card">
                    <span class="muted">
                        У этого ученика пока
                        нет сформированных документов.
                    </span>
                </div>
                """
            )

        else:

            parts.append(
                """
                <div
                    class="card"
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
                                    Сформировал
                                </th>

                                <th>
                                </th>
                            </tr>
                        </thead>

                        <tbody>
                """
            )

            for row in history:

                label = html.escape(
                    DOCUMENT_LABELS.get(
                        row["document_type"],
                        row["document_type"],
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

                if row["order_number"]:

                    order_text = (
                        "№ "
                        + str(
                            row[
                                "order_number"
                            ]
                        )
                    )

                    if row["order_date"]:

                        order_text += (
                            " от "
                            + human_date(
                                row[
                                    "order_date"
                                ]
                            )
                        )

                issue_date = human_date(
                    row["issue_date"]
                    or row[
                        "generated_at"
                    ]
                )

                author = html.escape(
                    row[
                        "generated_by_name"
                    ]
                    or "—"
                )

                download_url = url_for(
                    "post_document_history_download_v9_5",
                    history_id=row["id"],
                )

                parts.append(
                    f"""
                    <tr>
                        <td>
                            <strong>
                                {label}
                            </strong>
                        </td>

                        <td>
                            {html.escape(
                                certificate_number
                            )}
                        </td>

                        <td>
                            {html.escape(
                                order_text
                            )}
                        </td>

                        <td>
                            {html.escape(
                                issue_date
                            )}
                        </td>

                        <td>
                            {author}
                        </td>

                        <td>
                            <a
                                class="
                                    btn
                                    btn-secondary
                                    btn-small
                                "
                                href="{html.escape(
                                    download_url,
                                    quote=True
                                )}"
                            >
                                Скачать
                            </a>
                        </td>
                    </tr>
                    """
                )

            parts.append(
                """
                        </tbody>
                    </table>
                </div>
                """
            )

        parts.append(
            "</div>"
        )

        return "".join(
            parts
        )

    # ========================================================
    # ВСТАВЛЯЕМ ИСТОРИЮ НА СТРАНИЦУ
    # КОНКРЕТНОГО УЧЕНИКА
    # ========================================================

    @app.after_request
    def add_history_to_student_page(
        response,
    ):

        if request.method != "GET":
            return response

        student_id = (
            student_id_from_request()
        )

        if not student_id:
            return response

        # Только главная страница
        # документов ПО ЭТОМУ ученику.
        rule = (
            request.url_rule.rule
            if request.url_rule
            else ""
        )

        normalized = rule.rstrip("/")

        if normalized != (
            "/students/"
            "<int:student_id>/"
            "post-docs"
        ):
            return response

        if response.status_code != 200:
            return response

        if (
            "text/html"
            not in (
                response.content_type
                or ""
            )
        ):
            return response

        try:

            source = response.get_data(
                as_text=True
            )

            block = (
                build_student_history_html(
                    student_id
                )
            )

            # Добавляем перед концом
            # основного контейнера страницы.
            if "</section>" in source:

                source = source.replace(
                    "</section>",
                    block
                    + "</section>",
                    1,
                )

            elif "</main>" in source:

                source = source.replace(
                    "</main>",
                    block
                    + "</main>",
                    1,
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

        except Exception as error:

            print(
                "V9.5 history HTML:",
                repr(error),
            )

        return response

    # ========================================================
    # URL
    # ========================================================

    if (
        "post_document_generate_again_v9_5"
        not in app.view_functions
    ):

        app.add_url_rule(
            (
                "/students/"
                "<int:student_id>/"
                "post-docs/"
                "<document_type>/"
                "generate-again"
            ),

            endpoint=
                "post_document_generate_again_v9_5",

            view_func=roles_required(
                "attestation",
                "admin",
            )(
                generate_again
            ),

            methods=["POST"],
        )

    if (
        "post_document_history_download_v9_5"
        not in app.view_functions
    ):

        app.add_url_rule(
            (
                "/post-doc-history/"
                "<int:history_id>/download"
            ),

            endpoint=
                "post_document_history_download_v9_5",

            view_func=roles_required(
                "attestation",
                "admin",
            )(
                download_history_document
            ),

            methods=["GET"],
        )
