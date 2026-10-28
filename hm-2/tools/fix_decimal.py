from pathlib import Path

project_dir = Path(__file__).resolve().parents[1]
models_dir = project_dir / "generated/src/openapi_server/models"

if not models_dir.is_dir():
    raise SystemExit("Сначала выполните генерацию моделей.")

changed = 0

for file in models_dir.glob("*.py"):
    source = file.read_text(encoding="utf-8")
    updated = "".join(
        line.replace("strict=True", "strict=False")
        if "Annotated[Decimal, Field(" in line else line
        for line in source.splitlines(keepends=True)
    )

    if updated != source:
        file.write_text(updated, encoding="utf-8")
        changed += 1

print(f"Обновлено файлов: {changed}")
