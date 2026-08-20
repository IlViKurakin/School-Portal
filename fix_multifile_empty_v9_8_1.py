from pathlib import Path
import ast


PATCH_FILE = Path(
    "portal_multifile_upload_fix_v9_8.py"
)


def main():
    if not PATCH_FILE.exists():
        raise RuntimeError(
            "Не найден файл "
            "portal_multifile_upload_fix_v9_8.py"
        )

    text = PATCH_FILE.read_text(
        encoding="utf-8"
    )

    old = '''    data = upload.read()

    if not data:
'''

    new = '''    data = upload.read()

    # После чтения возвращаем указатель файла
    # в начало. Это необходимо потому, что
    # действующий обработчик portal_validation_v6
    # затем повторно читает request.files.
    try:
        upload.stream.seek(0)
    except Exception:
        pass

    if not data:
'''

    if new in text:
        print(
            "Исправление уже установлено."
        )
        return

    if old not in text:
        raise RuntimeError(
            "Не найден ожидаемый участок кода. "
            "Файл не изменён."
        )

    text = text.replace(
        old,
        new,
        1,
    )

    # Проверяем синтаксис до записи.
    ast.parse(
        text,
        filename=str(PATCH_FILE),
    )

    PATCH_FILE.write_text(
        text,
        encoding="utf-8",
    )

    print()
    print(
        "Исправление v9.8.1 установлено."
    )
    print(
        "Файловые потоки теперь возвращаются "
        "в начало после предварительного чтения."
    )
    print(
        "OCR и база данных не изменялись."
    )
    print(
        "Резервные копии не создавались."
    )


if __name__ == "__main__":
    main()