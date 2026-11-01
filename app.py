"""District Report — сайт обращений жителей района.

Запуск:  python app.py   →  http://127.0.0.1:5000
"""
from __future__ import annotations

import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("DATABASE", BASE_DIR / "main.db"))
PER_PAGE = 8
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))
MAX_PHOTO_MB = 5
LOCAL_UTC_OFFSET = int(os.environ.get("LOCAL_UTC_OFFSET", "5"))  # Казахстан, UTC+5

CATEGORIES = [
    "Дороги и тротуары",
    "Освещение",
    "Мусор и благоустройство",
    "Шум",
    "ЖКХ",
    "Транспорт",
    "Безопасность",
    "Другое",
]
DEFAULT_CATEGORY = "Другое"

DEFAULT_STATUS = "На рассмотрении"
STATUSES = {  # статус -> css-класс
    "На рассмотрении": "pending",
    "В работе": "in_progress",
    "Решено": "resolved",
    "Отклонено": "rejected",
}

SORTS = {
    "new": ("Сначала новые", "r.Created_at DESC, r.ID DESC"),
    "popular": ("Самые поддержанные", "COALESCE(r.counter_likes, 0) DESC, r.ID DESC"),
    "old": ("Сначала старые", "r.Created_at ASC, r.ID ASC"),
}

# SQL-выражения: старые строки могут содержать NULL в Status / type
STATUS_SQL = f"COALESCE(NULLIF(r.Status, ''), '{DEFAULT_STATUS}')"
TYPE_SQL = f"COALESCE(NULLIF(r.type, ''), '{DEFAULT_CATEGORY}')"

SELECT_REPORT = f"""
    SELECT r.ID, r.Title, r.Location, r.Description, r.User_id,
           COALESCE(r.counter_likes, 0) AS counter_likes,
           {STATUS_SQL} AS Status,
           {TYPE_SQL} AS type,
           r.Created_at, r.Photo,
           u.Name AS Author
    FROM Report r
    LEFT JOIN Users u ON u.ID = r.User_id
"""

app = Flask(__name__, template_folder="pages")
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    MAX_CONTENT_LENGTH=(MAX_PHOTO_MB + 1) * 1024 * 1024,  # запас на остальные поля формы
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    if not DATABASE.is_file():
        raise FileNotFoundError(f"База не найдена: {DATABASE}")
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # LIKE в SQLite не понимает регистр кириллицы — добавляем свою функцию
    conn.create_function("PYLOWER", 1, lambda s: s.lower() if isinstance(s, str) else s)
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


NEW_REPORT_TABLE = """
CREATE TABLE "Report" (
    "ID"            INTEGER,
    "Title"         TEXT,
    "Location"      TEXT,
    "Description"   TEXT,
    "User_id"       INTEGER REFERENCES "Users"("ID") ON DELETE SET NULL,
    "counter_likes" INTEGER NOT NULL DEFAULT 0,
    "Status"        TEXT DEFAULT 'На рассмотрении',
    "type"          TEXT,
    "Created_at"    TEXT DEFAULT CURRENT_TIMESTAMP,
    "Photo"         TEXT,
    PRIMARY KEY("ID" AUTOINCREMENT)
);
"""


def init_db() -> None:
    """Создаёт недостающие таблицы и мягко мигрирует старую таблицу Report.

    Что меняется в Report: убираем UNIQUE у User_id (иначе один житель мог бы
    оставить только одно обращение), чиним опечатку в статусе по умолчанию.
    Все существующие данные сохраняются.
    """
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS "Users" (
                "ID"            INTEGER PRIMARY KEY AUTOINCREMENT,
                "Name"          TEXT NOT NULL,
                "Email"         TEXT NOT NULL UNIQUE,
                "Password_hash" TEXT NOT NULL,
                "Is_admin"      INTEGER NOT NULL DEFAULT 0,
                "Created_at"    TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='Report'"
        ).fetchone()
        if row is None:
            conn.executescript(NEW_REPORT_TABLE)
        elif "UNIQUE" in row["sql"].upper() or "расмотрении" in row["sql"]:
            conn.executescript(
                "PRAGMA foreign_keys = OFF; BEGIN;"
                'ALTER TABLE "Report" RENAME TO "Report_old";'
                + NEW_REPORT_TABLE
                + """
                INSERT INTO "Report"
                    (ID, Title, Location, Description, User_id, counter_likes,
                     Status, type, Created_at)
                SELECT ID, Title, Location, Description, User_id,
                       COALESCE(counter_likes, 0),
                       CASE WHEN Status IS NULL OR Status = ''
                                 OR Status = 'На расмотрении'
                            THEN 'На рассмотрении' ELSE Status END,
                       type,
                       COALESCE(Created_at, CURRENT_TIMESTAMP)
                FROM "Report_old";
                DROP TABLE "Report_old";
                COMMIT; PRAGMA foreign_keys = ON;
                """
            )
        # Фото: добавляем колонку в уже существующую таблицу
        cols = {r["name"] for r in conn.execute('PRAGMA table_info("Report")')}
        if "Photo" not in cols:
            conn.execute('ALTER TABLE "Report" ADD COLUMN "Photo" TEXT')
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS "Report_like" (
                "Report_id" INTEGER NOT NULL REFERENCES "Report"("ID") ON DELETE CASCADE,
                "Voter"     TEXT NOT NULL,
                PRIMARY KEY("Report_id", "Voter")
            );
            CREATE INDEX IF NOT EXISTS idx_report_created ON "Report"("Created_at");
            """
        )
        # Администратор по умолчанию (для прототипа; смените пароль через переменные окружения)
        if not conn.execute('SELECT 1 FROM "Users" WHERE Is_admin = 1').fetchone():
            conn.execute(
                'INSERT INTO "Users" (Name, Email, Password_hash, Is_admin) VALUES (?,?,?,1)',
                (
                    "Администратор",
                    os.environ.get("ADMIN_EMAIL", "admin@districtreport.local"),
                    generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")),
                ),
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Пользователь, CSRF, вспомогательное
# --------------------------------------------------------------------------
@app.before_request
def load_user_and_check_csrf():
    session.permanent = True
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = get_db().execute(
            'SELECT ID, Name, Email, Is_admin FROM "Users" WHERE ID = ?', (uid,)
        ).fetchone()
        if g.user is None:
            session.pop("user_id", None)

    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Сессия устарела. Обновите страницу и попробуйте снова.")


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def current_voter() -> str:
    """Идентификатор голосующего: user-id или анонимный токен в cookie-сессии."""
    if g.user:
        return f"u{g.user['ID']}"
    if "anon" not in session:
        session["anon"] = secrets.token_hex(12)
    return f"a{session['anon']}"


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if g.user is None:
            flash("Войдите, чтобы продолжить.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if g.user is None or not g.user["Is_admin"]:
            abort(403)
        return view(*a, **kw)
    return wrapper


def detect_image_ext(head: bytes) -> str | None:
    """Определяет тип картинки по содержимому, а не по имени файла."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def save_photo(file) -> tuple[str | None, str | None]:
    """Сохраняет загруженное фото. Возвращает (имя_файла, ошибка)."""
    if file is None or not file.filename:
        return None, None
    data = file.read(MAX_PHOTO_MB * 1024 * 1024 + 1)
    if len(data) > MAX_PHOTO_MB * 1024 * 1024:
        return None, f"Фото слишком большое (максимум {MAX_PHOTO_MB} МБ)."
    ext = detect_image_ext(data[:16])
    if ext is None:
        return None, "Допустимы только изображения JPG, PNG, WebP или GIF."
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(16) + ext
    (UPLOAD_DIR / name).write_bytes(data)
    return name, None


def delete_photo_file(name: str | None) -> None:
    if name and Path(name).name == name:  # без путей
        try:
            (UPLOAD_DIR / name).unlink()
        except FileNotFoundError:
            pass


def safe_next(target: str | None) -> str:
    if target:
        p = urlparse(target)
        if not p.scheme and not p.netloc and target.startswith("/") and not target.startswith("//"):
            return target
    return url_for("home")


@app.template_filter("localdt")
def localdt(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    local = dt.astimezone(timezone(timedelta(hours=LOCAL_UTC_OFFSET)))
    return local.strftime("%d.%m.%Y, %H:%M")


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "STATUSES": STATUSES,
        "CATEGORIES": CATEGORIES,
        "page_url": page_url,
    }


def page_url(page: int) -> str:
    args = request.args.to_dict()
    args["page"] = page
    return url_for(request.endpoint, **args)


# --------------------------------------------------------------------------
# Запросы к отчётам
# --------------------------------------------------------------------------
def fetch_reports(q: str, category: str, status: str, sort: str, page: int):
    where, params = [], []

    if q:
        needle = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "(PYLOWER(r.Title) LIKE ? ESCAPE '\\' OR PYLOWER(r.Location) LIKE ? ESCAPE '\\' "
            "OR PYLOWER(r.Description) LIKE ? ESCAPE '\\')"
        )
        params += [needle] * 3
    if category:
        where.append(f"{TYPE_SQL} = ?")
        params.append(category)
    if status:
        where.append(f"{STATUS_SQL} = ?")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    db = get_db()

    total = db.execute(f"SELECT COUNT(*) FROM Report r {where_sql}", params).fetchone()[0]
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(page, 1), pages)

    order = SORTS.get(sort, SORTS["new"])[1]
    rows = db.execute(
        f"{SELECT_REPORT} {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()
    reports = [dict(r) for r in rows]

    liked_ids = set()
    if reports:
        ids = [r["ID"] for r in reports]
        marks = ",".join("?" * len(ids))
        liked_ids = {
            r[0] for r in db.execute(
                f"SELECT Report_id FROM Report_like WHERE Voter = ? AND Report_id IN ({marks})",
                [current_voter(), *ids],
            )
        }
    for r in reports:
        r["liked"] = r["ID"] in liked_ids
        r["status_class"] = STATUSES.get(r["Status"], "pending")
    return reports, total, page, pages


def fetch_stats() -> dict:
    row = get_db().execute(
        f"""SELECT COUNT(*) AS total,
                   COALESCE(SUM({STATUS_SQL} = 'Решено'), 0) AS resolved,
                   COALESCE(SUM({STATUS_SQL} = 'В работе'), 0) AS in_progress,
                   COALESCE(SUM({STATUS_SQL} = '{DEFAULT_STATUS}'), 0) AS pending
            FROM Report r"""
    ).fetchone()
    return dict(row)


def get_report_or_404(report_id: int) -> dict:
    row = get_db().execute(f"{SELECT_REPORT} WHERE r.ID = ?", (report_id,)).fetchone()
    if row is None:
        abort(404)
    r = dict(row)
    r["status_class"] = STATUSES.get(r["Status"], "pending")
    r["liked"] = bool(get_db().execute(
        "SELECT 1 FROM Report_like WHERE Report_id = ? AND Voter = ?",
        (report_id, current_voter()),
    ).fetchone())
    return r


# --------------------------------------------------------------------------
# Страницы
# --------------------------------------------------------------------------
@app.route("/")
def home():
    q = request.args.get("q", "").strip()[:100]
    category = request.args.get("category", "")
    status = request.args.get("status", "")
    sort = request.args.get("sort", "new")
    page = request.args.get("page", 1, type=int)

    if category not in CATEGORIES:
        category = ""
    if status not in STATUSES:
        status = ""
    if sort not in SORTS:
        sort = "new"

    reports, total, page, pages = fetch_reports(q, category, status, sort, page)
    return render_template(
        "District_report.html",
        reports=reports, total=total, page=page, pages=pages,
        q=q, category=category, status=status, sort=sort, sorts=SORTS,
        stats=fetch_stats(),
    )


@app.route("/report/new", methods=["GET", "POST"])
def new_report():
    form = {"title": "", "location": "", "category": "", "description": ""}
    errors: dict[str, str] = {}

    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        if not 3 <= len(form["title"]) <= 100:
            errors["title"] = "Заголовок: от 3 до 100 символов."
        if not 2 <= len(form["location"]) <= 120:
            errors["location"] = "Укажите место: от 2 до 120 символов."
        if form["category"] not in CATEGORIES:
            errors["category"] = "Выберите категорию."
        if not 10 <= len(form["description"]) <= 2000:
            errors["description"] = "Описание: от 10 до 2000 символов."

        photo_name = None
        if not errors:
            photo_name, photo_error = save_photo(request.files.get("photo"))
            if photo_error:
                errors["photo"] = photo_error

        if not errors:
            db = get_db()
            try:
                cur = db.execute(
                    """INSERT INTO Report (Title, Location, Description, User_id, counter_likes,
                                           Status, type, Photo)
                       VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                    (form["title"], form["location"], form["description"],
                     g.user["ID"] if g.user else None, DEFAULT_STATUS, form["category"],
                     photo_name),
                )
                db.commit()
            except Exception:
                delete_photo_file(photo_name)
                raise
            flash("Спасибо! Обращение отправлено и появилось в списке.", "success")
            return redirect(url_for("report_detail", report_id=cur.lastrowid))

    return render_template("report_form.html", form=form, errors=errors)


@app.route("/report/<int:report_id>")
def report_detail(report_id: int):
    return render_template("report_detail.html", report=get_report_or_404(report_id))


@app.post("/report/<int:report_id>/like")
def like_report(report_id: int):
    db = get_db()
    if not db.execute("SELECT 1 FROM Report WHERE ID = ?", (report_id,)).fetchone():
        abort(404)
    voter = current_voter()
    removed = db.execute(
        "DELETE FROM Report_like WHERE Report_id = ? AND Voter = ?", (report_id, voter)
    ).rowcount
    if removed:
        db.execute(
            "UPDATE Report SET counter_likes = MAX(COALESCE(counter_likes, 0) - 1, 0) WHERE ID = ?",
            (report_id,),
        )
    else:
        db.execute("INSERT INTO Report_like (Report_id, Voter) VALUES (?, ?)", (report_id, voter))
        db.execute(
            "UPDATE Report SET counter_likes = COALESCE(counter_likes, 0) + 1 WHERE ID = ?",
            (report_id,),
        )
    db.commit()
    likes = db.execute("SELECT counter_likes FROM Report WHERE ID = ?", (report_id,)).fetchone()[0]
    return jsonify(liked=not removed, likes=likes)


@app.post("/report/<int:report_id>/status")
@admin_required
def change_status(report_id: int):
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400, "Неизвестный статус.")
    db = get_db()
    db.execute("UPDATE Report SET Status = ? WHERE ID = ?", (status, report_id))
    db.commit()
    flash(f"Статус изменён: {status}.", "success")
    return redirect(url_for("report_detail", report_id=report_id))


@app.post("/report/<int:report_id>/delete")
@admin_required
def delete_report(report_id: int):
    db = get_db()
    row = db.execute("SELECT Photo FROM Report WHERE ID = ?", (report_id,)).fetchone()
    db.execute("DELETE FROM Report WHERE ID = ?", (report_id,))
    db.commit()
    if row:
        delete_photo_file(row["Photo"])
    flash("Обращение удалено.", "info")
    return redirect(url_for("home"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    response = send_from_directory(UPLOAD_DIR, filename, max_age=60 * 60 * 24 * 30)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


# --------------------------------------------------------------------------
# Авторизация
# --------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("home"))
    form, errors = {"name": "", "email": ""}, {}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["email"] = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not 2 <= len(form["name"]) <= 60:
            errors["name"] = "Имя: от 2 до 60 символов."
        if "@" not in form["email"] or len(form["email"]) > 120:
            errors["email"] = "Введите корректный email."
        if len(password) < 6:
            errors["password"] = "Пароль: минимум 6 символов."
        elif password != request.form.get("password2", ""):
            errors["password"] = "Пароли не совпадают."
        db = get_db()
        if not errors and db.execute(
            'SELECT 1 FROM "Users" WHERE Email = ?', (form["email"],)
        ).fetchone():
            errors["email"] = "Этот email уже зарегистрирован."
        if not errors:
            cur = db.execute(
                'INSERT INTO "Users" (Name, Email, Password_hash) VALUES (?,?,?)',
                (form["name"], form["email"], generate_password_hash(password)),
            )
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
            flash(f"Добро пожаловать, {form['name']}!", "success")
            return redirect(url_for("home"))
    return render_template("register.html", form=form, errors=errors)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("home"))
    email, error = "", ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute(
            'SELECT * FROM "Users" WHERE Email = ?', (email,)
        ).fetchone()
        if user and check_password_hash(user["Password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["ID"]
            flash(f"С возвращением, {user['Name']}!", "success")
            return redirect(safe_next(request.args.get("next")))
        error = "Неверный email или пароль."
    return render_template("login.html", email=email, error=error)


@app.post("/logout")
def logout():
    session.clear()
    flash("Вы вышли из аккаунта.", "info")
    return redirect(url_for("home"))


@app.route("/my")
@login_required
def my_reports():
    rows = get_db().execute(
        f"{SELECT_REPORT} WHERE r.User_id = ? ORDER BY r.Created_at DESC, r.ID DESC",
        (g.user["ID"],),
    ).fetchall()
    reports = []
    for row in rows:
        r = dict(row)
        r["status_class"] = STATUSES.get(r["Status"], "pending")
        r["liked"] = False
        reports.append(r)
    return render_template("my_reports.html", reports=reports)


# --------------------------------------------------------------------------
# Ошибки
# --------------------------------------------------------------------------
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(err):
    messages = {
        400: err.description if getattr(err, "description", None) else "Некорректный запрос.",
        403: "У вас нет доступа к этой странице.",
        404: "Такой страницы не существует.",
        413: f"Файл слишком большой. Максимальный размер фото — {MAX_PHOTO_MB} МБ.",
    }
    if request.path.endswith("/like"):
        return jsonify(error=messages[err.code]), err.code
    return render_template("error.html", code=err.code, message=messages[err.code]), err.code


init_db()

if __name__ == "__main__":
    app.run(debug=True)
