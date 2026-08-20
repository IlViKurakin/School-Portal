from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_PATH = ROOT / "app.py"
PATCH_PATH = ROOT / "portal_multifile_upload_fix_v9_8.py"

MARKER = "MULTIFILE UPLOAD FIX V9.8"


PATCH_SOURCE = r'''
from __future__ import annotations

import io
import re
from pathlib import Path

from flask import flash, request
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from werkzeug.datastructures import FileStorage


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
}

MAX_FILES = 30
MAX_FILE_SIZE = 25 * 1024 * 1024


def _clean_name(value: str) -> str:
    value = str(value or "").replace("\\", "/")
    return Path(value).name.strip() or "document"


def _read_upload(upload):
    name = _clean_name(upload.filename)
    extension = Path(name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Файл «{name}»: разрешены только "
            "PDF, JPG, JPEG и PNG."
        )

    data = upload.read()

    if not data:
        raise ValueError(
            f"Файл «{name}» пустой."
        )

    if len(data) > MAX_FILE_SIZE:
        raise ValueError(
            f"Файл «{name}» больше 25 МБ."
        )

    return name, extension, data


def _image_reader(data: bytes) -> PdfReader:
    try:
        image = Image.open(
            io.BytesIO(data)
        )

        image = ImageOps.exif_transpose(
            image
        )

        image.load()

    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as error:
        raise ValueError(
            "Не удалось прочитать одно из изображений."
        ) from error

    if image.mode != "RGB":
        image = image.convert("RGB")

    output = io.BytesIO()

    image.save(
        output,
        format="PDF",
        resolution=150.0,
    )

    output.seek(0)

    return PdfReader(output)


def _merge_uploads(
    uploads,
) -> tuple[bytes, int, list[str]]:

    if len(uploads) > MAX_FILES:
        raise ValueError(
            f"За один раз можно загрузить "
            f"не более {MAX_FILES} файлов."
        )

    writer = PdfWriter()

    page_count = 0
    names = []

    for upload in uploads:
        (
            name,
            extension,
            data,
        ) = _read_upload(upload)

        names.append(name)

        if extension == ".pdf":
            try:
                reader = PdfReader(
                    io.BytesIO(data)
                )

                if reader.is_encrypted:
                    raise ValueError(
                        f"PDF «{name}» защищён паролем."
                    )

            except ValueError:
                raise

            except Exception as error:
                raise ValueError(
                    f"Не удалось прочитать PDF «{name}»."
                ) from error

        else:
            reader = _image_reader(
                data
            )

        for page in reader.pages:
            writer.add_page(page)
            page_count += 1

    if page_count == 0:
        raise ValueError(
            "В выбранных файлах нет страниц."
        )

    result = io.BytesIO()

    writer.write(
        result
    )

    return (
        result.getvalue(),
        page_count,
        names,
    )


def _replace_upload_field(html: str) -> str:
    """
    Меняем старое одnofайловое поле,
    не вмешиваясь в остальную страницу.
    """

    if (
        'name="files"' in html
        and "multiple" in html
    ):
        return html

    # Старое имя file -> files
    html = re.sub(
        r'name\s*=\s*["\']file["\']',
        'name="files"',
        html,
        count=1,
        flags=re.IGNORECASE,
    )

    # Добавляем multiple в первый input type=file.
    def add_multiple(match):
        tag = match.group(0)

        if re.search(
            r"\bmultiple\b",
            tag,
            flags=re.IGNORECASE,
        ):
            return tag

        return tag[:-1] + " multiple>"

    html = re.sub(
        r'<input\b[^>]*'
        r'type\s*=\s*["\']file["\']'
        r'[^>]*>',
        add_multiple,
        html,
        count=1,
        flags=re.IGNORECASE,
    )

    html = html.replace(
        "<label>Новый файл</label>",
        "<label>Новый файл или несколько страниц</label>",
        1,
    )

    html = html.replace(
        "PDF, JPG, JPEG или PNG. Не более 25 МБ.",
        (
            "Можно выбрать до 30 PDF, JPG, JPEG или PNG. "
            "Каждый файл — не более 25 МБ. "
            "Выбранные страницы будут объединены "
            "в один документ."
        ),
        1,
    )

    return html


def apply_multifile_upload_fix_v9_8(
    app,
    namespace,
):
    """
    ВАЖНО:
    этот патч должен применяться ПОСЛЕДНИМ.

    Мы не заменяем текущую OCR-логику.
    Мы только преобразуем несколько выбранных
    страниц в один PDF и передаём его действующему
    upload_document.
    """

    original_view = app.view_functions.get(
        "upload_document"
    )

    if original_view is None:
        raise RuntimeError(
            "Не найден endpoint upload_document."
        )

    if getattr(
        original_view,
        "_multifile_upload_v98",
        False,
    ):
        return

    roles_required = namespace[
        "roles_required"
    ]

    def multifile_upload_view(
        student_id: int,
        document_type: str,
    ):
        # ----------------------------------------------
        # POST
        # ----------------------------------------------
        if request.method == "POST":

            uploads = [
                item
                for item
                in request.files.getlist(
                    "files"
                )
                if (
                    item
                    and item.filename
                )
            ]

            # Поле files существует после нашего GET.
            if uploads:

                try:
                    if len(uploads) == 1:
                        # Один файл тоже поддерживаем.
                        # Передаём его старому endpoint
                        # под ожидаемым именем "file".
                        replacement = uploads[0]

                    else:
                        (
                            merged_data,
                            page_count,
                            original_names,
                        ) = _merge_uploads(
                            uploads
                        )

                        stream = io.BytesIO(
                            merged_data
                        )

                        replacement = FileStorage(
                            stream=stream,
                            filename=(
                                "multi_page_document.pdf"
                            ),
                            content_type=(
                                "application/pdf"
                            ),
                        )

                    # request.files у Flask immutable.
                    # Создаём изменяемую копию и
                    # подсовываем текущему endpoint
                    # объединённый PDF как обычный file.
                    new_files = request.files.copy()

                    new_files.setlist(
                        "file",
                        [
                            replacement
                        ],
                    )

                    request.__dict__[
                        "files"
                    ] = new_files

                except ValueError as error:
                    flash(
                        str(error),
                        "error",
                    )

                    # Текущий endpoint при отсутствии
                    # file сам вернёт пользователя
                    # на страницу загрузки.
                    new_files = (
                        request.files.copy()
                    )

                    new_files.setlist(
                        "file",
                        [],
                    )

                    request.__dict__[
                        "files"
                    ] = new_files

        # ----------------------------------------------
        # Вызываем ТЕКУЩИЙ обработчик.
        #
        # Поэтому сохраняются:
        # - текущий OCR;
        # - текущие проверки;
        # - версия документа;
        # - аудит;
        # - существующая БД.
        # ----------------------------------------------

        result = original_view(
            student_id=student_id,
            document_type=document_type,
        )

        response = app.make_response(
            result
        )

        # ----------------------------------------------
        # GET: меняем только поле выбора файлов.
        # ----------------------------------------------
        if (
            request.method == "GET"
            and response.status_code == 200
            and response.mimetype
            == "text/html"
        ):
            html = response.get_data(
                as_text=True
            )

            html = _replace_upload_field(
                html
            )

            response.set_data(
                html
            )

            response.headers[
                "Content-Length"
            ] = str(
                len(
                    response.get_data()
                )
            )

        return response

    wrapped = roles_required(
        "branch"
    )(
        multifile_upload_view
    )

    wrapped.__name__ = (
        "multifile_upload_view_v9_8"
    )

    wrapped._multifile_upload_v98 = True

    app.view_functions[
        "upload_document"
    ] = wrapped
'''


def main():
    if not APP_PATH.exists():
        raise RuntimeError(
            "Не найден C:\\school-portal\\app.py"
        )

    app_text = APP_PATH.read_text(
        encoding="utf-8"
    )

    # -----------------------------------------------
    # Пишем новый модуль
    # -----------------------------------------------

    PATCH_PATH.write_text(
        PATCH_SOURCE,
        encoding="utf-8"
    )

    # -----------------------------------------------
    # Проверяем новый модуль
    # -----------------------------------------------

    ast.parse(
        PATCH_SOURCE,
        filename=PATCH_PATH.name,
    )

    import_block = """
# MULTIFILE UPLOAD FIX V9.8
from portal_multifile_upload_fix_v9_8 import apply_multifile_upload_fix_v9_8
apply_multifile_upload_fix_v9_8(app, globals())

"""

    if MARKER not in app_text:

        launch_marker = (
            'if __name__ == "__main__":'
        )

        position = app_text.rfind(
            launch_marker
        )

        if position < 0:
            raise RuntimeError(
                "В app.py не найден блок запуска."
            )

        # Устанавливаем непосредственно перед
        # if __name__ == "__main__".
        # Значит, этот upload patch будет последним.
        app_text = (
            app_text[:position]
            + import_block
            + app_text[position:]
        )

        ast.parse(
            app_text,
            filename="app.py",
        )

        APP_PATH.write_text(
            app_text,
            encoding="utf-8"
        )

    print()
    print(
        "Многофайловая загрузка установлена."
    )
    print()
    print(
        "Теперь один раздел документа поддерживает:"
    )
    print(
        "- до 30 файлов за один раз;"
    )
    print(
        "- PDF, JPG, JPEG, PNG;"
    )
    print(
        "- объединение страниц в один PDF;"
    )
    print(
        "- действующий OCR после объединения."
    )
    print()
    print(
        "Другие маршруты портала не менялись."
    )
    print(
        "portal.db не изменялась."
    )
    print(
        "Резервные копии не создавались."
    )


if __name__ == "__main__":
    main()