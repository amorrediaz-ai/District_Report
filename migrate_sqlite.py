"""One-time, all-or-nothing import into an EMPTY PostgreSQL database.

Set DATABASE_URL, then run:
    python migrate_sqlite.py --sqlite main.db --uploads uploads
The SQLite file is opened read-only. Existing PostgreSQL records are never replaced.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

from database import connect_db, initialize_schema


def timestamp(value):
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_legacy_photo(name, folder: Path, skip_missing: bool):
    if not name:
        return None, None, None
    path = (folder / name).resolve()
    if Path(name).name != name or path.parent != folder.resolve():
        raise ValueError("Недопустимый путь фото в SQLite. Импорт отменён.")
    if not path.is_file():
        if skip_missing:
            return None, None, None
        raise ValueError(
            f"Не найдено фото {name}. Укажите правильную папку --uploads "
            "или явно разрешите пропуск: --skip-missing-photos."
        )
    with path.open("rb") as handle:
        data = handle.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError(f"Фото {name} больше 5 МБ. Импорт отменён.")
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError(f"Фото {name} имеет неподдерживаемый формат.")
    return name, data, mime


def migrate(source: Path, uploads: Path, skip_missing_photos=False) -> dict:
    if not source.is_file():
        raise ValueError("Файл SQLite не найден. Укажите его через --sqlite.")
    source_conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    source_conn.row_factory = sqlite3.Row
    try:
        # Keep a single source snapshot if another local process is using SQLite.
        source_conn.execute("BEGIN")
        tables = {row[0].lower() for row in source_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"users", "report"}.issubset(tables):
            raise ValueError("В SQLite нужны таблицы Users и Report текущего прототипа.")
        users = [dict(row) for row in source_conn.execute('SELECT * FROM "Users"')]
        reports = [dict(row) for row in source_conn.execute('SELECT * FROM "Report"')]
        likes = [dict(row) for row in source_conn.execute('SELECT * FROM "Report_like"')] if "report_like" in tables else []
    finally:
        source_conn.close()

    result = {"users": len(users), "reports": len(reports), "likes": len(likes),
              "photos": 0, "skipped_photos": 0}
    with connect_db() as target, target.transaction():
        initialize_schema(target)
        # Also block concurrent inserts during the emptiness check and import.
        target.execute("LOCK TABLE users, report, report_like IN ACCESS EXCLUSIVE MODE")
        if target.execute("""SELECT 1 FROM users UNION ALL SELECT 1 FROM report
                             UNION ALL SELECT 1 FROM report_like LIMIT 1""").fetchone():
            raise ValueError(
                "PostgreSQL уже содержит данные. Импорт разрешён только в пустую базу. "
                "Запустите перенос до первого запуска сайта; существующие данные не изменены."
            )
        for row in users:
            target.execute(
                """INSERT INTO users (id, name, email, password_hash, is_admin, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (row["ID"], row["Name"], row["Email"].strip().lower(),
                 row["Password_hash"], bool(row.get("Is_admin")), timestamp(row.get("Created_at"))),
            )
        for row in reports:
            name, data, mime = read_legacy_photo(row.get("Photo"), uploads, skip_missing_photos)
            result["photos"] += int(name is not None)
            result["skipped_photos"] += int(bool(row.get("Photo")) and name is None)
            status = row.get("Status") or "На рассмотрении"
            if status == "На расмотрении":
                status = "На рассмотрении"
            target.execute(
                """INSERT INTO report
                   (id, title, location, description, user_id, counter_likes,
                    status, type, created_at, photo, photo_data, photo_mime)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (row["ID"], row.get("Title"), row.get("Location"), row.get("Description"),
                 row.get("User_id"), max(0, row.get("counter_likes") or 0), status,
                 row.get("type") or "Другое", timestamp(row.get("Created_at")), name, data, mime),
            )
        for row in likes:
            target.execute(
                "INSERT INTO report_like (report_id, voter) VALUES (%s, %s)",
                (row["Report_id"], row["Voter"]),
            )
        # Keep historical counts, but never let an imported count understate saved votes.
        target.execute("""UPDATE report r SET counter_likes = GREATEST(counter_likes,
                          (SELECT COUNT(*)::integer FROM report_like l WHERE l.report_id = r.id))""")
        # Explicit IDs do not advance identity sequences automatically.
        for table in ("users", "report"):
            maximum = target.execute(
                sql.SQL("SELECT COALESCE(MAX(id), 0) AS maximum FROM {}").format(sql.Identifier(table))
            ).fetchone()["maximum"]
            sequence = target.execute(
                "SELECT pg_get_serial_sequence(%s, 'id') AS name", (table,)
            ).fetchone()["name"]
            target.execute("SELECT setval(%s::regclass, %s, %s)",
                           (sequence, max(1, maximum), maximum > 0))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=Path("main.db"))
    parser.add_argument("--uploads", type=Path, default=Path("uploads"))
    parser.add_argument("--skip-missing-photos", action="store_true")
    args = parser.parse_args()
    try:
        counts = migrate(args.sqlite, args.uploads, args.skip_missing_photos)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Импорт отменён: {exc}\n")
    except psycopg.Error as exc:
        # Avoid logging connection credentials or personal data in SQL error details.
        parser.exit(1, f"Импорт отменён: PostgreSQL {type(exc).__name__}. "
                       "Проверьте подключение и целостность исходных данных.\n")
    print("Перенос завершён:", counts)


if __name__ == "__main__":
    main()
