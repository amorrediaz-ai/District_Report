"""District Report — сайт обращений жителей района.

Запуск:  python app.py   →  http://127.0.0.1:5000
"""
from __future__ import annotations

import csv
import hashlib
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlparse

import click
import psycopg
from psycopg.errors import UniqueViolation
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from database import connect_db, initialize_schema

PER_PAGE = 8
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
    SELECT r.id AS "ID", r.title AS "Title", r.location AS "Location",
           r.description AS "Description", r.user_id AS "User_id",
           COALESCE(r.counter_likes, 0) AS counter_likes,
           {STATUS_SQL} AS "Status",
           {TYPE_SQL} AS type,
           r.created_at AS "Created_at", r.photo AS "Photo",
           u.name AS "Author"
    FROM Report r
    LEFT JOIN Users u ON u.ID = r.User_id
"""

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if len(SECRET_KEY) < 32 or SECRET_KEY == "dev-secret-change-me":
    raise RuntimeError("Укажите постоянный SECRET_KEY длиной не менее 32 символов.")

app = Flask(__name__, template_folder="pages")
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=(MAX_PHOTO_MB + 1) * 1024 * 1024,  # запас на остальные поля формы
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("RENDER", "").lower() == "true",
    SESSION_COOKIE_SAMESITE="Lax",
)


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------
def get_db() -> psycopg.Connection:
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Create PostgreSQL tables without deleting existing data."""
    with connect_db() as conn, conn.transaction():
        initialize_schema(conn)
        if not conn.execute("SELECT 1 FROM users WHERE is_admin").fetchone():
            email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
            password = os.environ.get("ADMIN_PASSWORD", "")
            if "@" not in email or len(password) < 12:
                raise RuntimeError(
                    "Для первого запуска задайте ADMIN_EMAIL и ADMIN_PASSWORD "
                    "(пароль не менее 12 символов)."
                )
            conn.execute(
                """INSERT INTO users (name, email, password_hash, is_admin)
                   VALUES (%s, %s, %s, TRUE)""",
                ("Администратор", email, generate_password_hash(password)),
            )


@app.cli.command("set-admin-password")
@click.argument("email")
@click.password_option(confirmation_prompt=True)
def set_admin_password(email, password):
    """Change an existing administrator's password, for example after import."""
    if len(password) < 12:
        raise click.ClickException("Пароль должен содержать не менее 12 символов.")
    with connect_db() as conn:
        row = conn.execute(
            """UPDATE users SET password_hash = %s
               WHERE lower(email) = %s AND is_admin RETURNING id""",
            (generate_password_hash(password), email.strip().lower()),
        ).fetchone()
    if row is None:
        raise click.ClickException("Администратор с этим email не найден.")
    click.echo("Пароль администратора изменён.")


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
            'SELECT id AS "ID", name AS "Name", email AS "Email", '
            'is_admin AS "Is_admin" FROM users WHERE id = %s', (uid,)
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


IMAGE_MIMES = {".jpg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp"}


def read_photo(file) -> tuple[dict | None, str | None]:
    """Validate the upload; the caller saves bytes with the report transaction."""
    if file is None or not file.filename:
        return None, None
    data = file.read(MAX_PHOTO_MB * 1024 * 1024 + 1)
    if len(data) > MAX_PHOTO_MB * 1024 * 1024:
        return None, f"Фото слишком большое (максимум {MAX_PHOTO_MB} МБ)."
    ext = detect_image_ext(data[:16])
    if ext is None:
        return None, "Допустимы только изображения JPG, PNG, WebP или GIF."
    return {"name": secrets.token_hex(16) + ext,
            "data": data, "mime": IMAGE_MIMES[ext]}, None


def safe_next(target: str | None) -> str:
    if target:
        p = urlparse(target)
        if not p.scheme and not p.netloc and target.startswith("/") and not target.startswith("//"):
            return target
    return url_for("home")


@app.template_filter("localdt")
def localdt(value: datetime | str | None) -> str:
    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return str(value)
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
def report_filters(q: str, category: str, status: str):
    where, params = [], []

    if q:
        needle = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "(r.Title ILIKE %s ESCAPE '\\' OR r.Location ILIKE %s ESCAPE '\\' "
            "OR r.Description ILIKE %s ESCAPE '\\')"
        )
        params += [needle] * 3
    if category:
        where.append(f"{TYPE_SQL} = %s")
        params.append(category)
    if status:
        where.append(f"{STATUS_SQL} = %s")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def fetch_reports(q: str, category: str, status: str, sort: str, page: int):
    where_sql, params = report_filters(q, category, status)
    db = get_db()

    total = db.execute(f"SELECT COUNT(*) AS total FROM Report r {where_sql}", params).fetchone()["total"]
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(page, 1), pages)

    order = SORTS.get(sort, SORTS["new"])[1]
    rows = db.execute(
        f"{SELECT_REPORT} {where_sql} ORDER BY {order} LIMIT %s OFFSET %s",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()
    reports = [dict(r) for r in rows]

    liked_ids = set()
    if reports:
        ids = [r["ID"] for r in reports]
        marks = ",".join(["%s"] * len(ids))
        liked_ids = {
            r["report_id"] for r in db.execute(
                f"SELECT Report_id FROM Report_like WHERE Voter = %s AND Report_id IN ({marks})",
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
                   COUNT(*) FILTER (WHERE {STATUS_SQL} = 'Решено') AS resolved,
                   COUNT(*) FILTER (WHERE {STATUS_SQL} = 'В работе') AS in_progress,
                   COUNT(*) FILTER (WHERE {STATUS_SQL} = '{DEFAULT_STATUS}') AS pending
            FROM Report r"""
    ).fetchone()
    return dict(row)


def get_report_or_404(report_id: int) -> dict:
    row = get_db().execute(f"{SELECT_REPORT} WHERE r.ID = %s", (report_id,)).fetchone()
    if row is None:
        abort(404)
    r = dict(row)
    r["status_class"] = STATUSES.get(r["Status"], "pending")
    r["liked"] = bool(get_db().execute(
        "SELECT 1 FROM Report_like WHERE Report_id = %s AND Voter = %s",
        (report_id, current_voter()),
    ).fetchone())
    return r


# --------------------------------------------------------------------------
# Страницы
# --------------------------------------------------------------------------
def read_filters():
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
    return q, category, status, sort, page


def category_statistics():
    rows = get_db().execute(
        f"SELECT {TYPE_SQL} AS category, COUNT(*) AS total FROM report r "
        f"GROUP BY {TYPE_SQL} ORDER BY total DESC, category"
    ).fetchall()
    maximum = max((row["total"] for row in rows), default=1)
    return [dict(row, percent=round(row["total"] * 100 / maximum)) for row in rows]


@app.route("/")
def home():
    q, category, status, sort, page = read_filters()

    reports, total, page, pages = fetch_reports(q, category, status, sort, page)
    return render_template(
        "District_report.html",
        reports=reports, total=total, page=page, pages=pages,
        q=q, category=category, status=status, sort=sort, sorts=SORTS,
        stats=fetch_stats(), category_stats=category_statistics(),
    )


@app.get("/admin")
@admin_required
def admin_dashboard():
    q, category, status, sort, page = read_filters()
    reports, total, page, pages = fetch_reports(q, category, status, sort, page)
    return render_template(
        "admin.html", reports=reports, total=total, page=page, pages=pages,
        q=q, category=category, status=status, sort=sort, sorts=SORTS,
        stats=fetch_stats(),
    )


def csv_cell(value):
    """Keep user text from being interpreted as a spreadsheet formula."""
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@app.get("/admin/export.csv")
@admin_required
def export_reports():
    q, category, status, sort, _ = read_filters()
    where_sql, params = report_filters(q, category, status)
    rows = get_db().execute(
        f"{SELECT_REPORT} {where_sql} ORDER BY {SORTS[sort][1]}", params
    )
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Номер", "Заголовок", "Категория", "Место", "Статус", "Создано", "Поддержали"])
    for row in rows:
        writer.writerow([row["ID"], csv_cell(row["Title"]), csv_cell(row["type"]),
                         csv_cell(row["Location"]), csv_cell(row["Status"]),
                         localdt(row["Created_at"]), row["counter_likes"]])
    response = app.response_class("\ufeff" + output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="district-reports.csv"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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

        photo = None
        if not errors:
            photo, photo_error = read_photo(request.files.get("photo"))
            if photo_error:
                errors["photo"] = photo_error

        if not errors:
            db = get_db()
            with db.transaction():
                row = db.execute(
                    """INSERT INTO report
                       (title, location, description, user_id, status, type,
                        photo, photo_data, photo_mime)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (form["title"], form["location"], form["description"],
                     g.user["ID"] if g.user else None, DEFAULT_STATUS, form["category"],
                     photo["name"] if photo else None,
                     photo["data"] if photo else None,
                     photo["mime"] if photo else None),
                ).fetchone()
            flash("Спасибо! Обращение отправлено и появилось в списке.", "success")
            return redirect(url_for("report_detail", report_id=row["id"]))

    return render_template("report_form.html", form=form, errors=errors)


@app.route("/report/<int:report_id>")
def report_detail(report_id: int):
    report = get_report_or_404(report_id)
    updates = get_db().execute(
        "SELECT old_status, new_status, message, created_at FROM report_update "
        "WHERE report_id = %s ORDER BY id", (report_id,),
    ).fetchall()
    return render_template("report_detail.html", report=report, updates=updates)


@app.post("/report/<int:report_id>/like")
def like_report(report_id: int):
    db = get_db()
    voter = current_voter()
    with db.transaction():
        # Serialize toggles on this report so simultaneous clicks cannot lose votes.
        if not db.execute(
            "SELECT id FROM report WHERE id = %s FOR UPDATE", (report_id,)
        ).fetchone():
            abort(404)
        removed = db.execute(
            "DELETE FROM report_like WHERE report_id = %s AND voter = %s",
            (report_id, voter),
        ).rowcount
        if not removed:
            db.execute(
                "INSERT INTO report_like (report_id, voter) VALUES (%s, %s)",
                (report_id, voter),
            )
        row = db.execute(
            """UPDATE report SET counter_likes = GREATEST(counter_likes + %s, 0)
               WHERE id = %s RETURNING counter_likes""",
            (-1 if removed else 1, report_id),
        ).fetchone()
    return jsonify(liked=not removed, likes=row["counter_likes"])


@app.post("/report/<int:report_id>/status")
@admin_required
def change_status(report_id: int):
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400, "Неизвестный статус.")
    message = request.form.get("message", "").strip()
    if len(message) > 2000:
        abort(400, "Ответ: не более 2000 символов.")
    db = get_db()
    with db.transaction():
        row = db.execute(
            f'SELECT {STATUS_SQL} AS status FROM report r WHERE id = %s FOR UPDATE',
            (report_id,),
        ).fetchone()
        if row is None:
            abort(404)
        # Old clients may submit only a status. No-op submissions add no history.
        if row["status"] == status and not message:
            flash("Изменений нет. Выберите другой статус или напишите ответ.", "info")
        else:
            db.execute("UPDATE report SET status = %s WHERE id = %s", (status, report_id))
            db.execute(
                """INSERT INTO report_update (report_id, actor_id, old_status, new_status, message)
                   VALUES (%s, %s, %s, %s, %s)""",
                (report_id, g.user["ID"], row["status"], status, message),
            )
            flash("Изменения опубликованы в истории обращения.", "success")
    return redirect(url_for("report_detail", report_id=report_id))


@app.post("/report/<int:report_id>/delete")
@admin_required
def delete_report(report_id: int):
    db = get_db()
    row = db.execute(
        "DELETE FROM report WHERE id = %s RETURNING id", (report_id,)
    ).fetchone()
    if row is None:
        abort(404)
    # The photo is in the same row, and report_like has ON DELETE CASCADE.
    flash("Обращение удалено.", "info")
    return redirect(url_for("home"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    if Path(filename).name != filename:
        abort(404)
    photo = get_db().execute(
        "SELECT photo_data, photo_mime FROM report WHERE photo = %s", (filename,)
    ).fetchone()
    if photo is None:
        abort(404)
    data = bytes(photo["photo_data"])
    response = send_file(
        BytesIO(data), mimetype=photo["photo_mime"], download_name=filename,
        max_age=60 * 60 * 24 * 30, conditional=True,
        etag=hashlib.sha256(data).hexdigest(),
    )
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
            'SELECT 1 FROM users WHERE Email = %s', (form["email"],)
        ).fetchone():
            errors["email"] = "Этот email уже зарегистрирован."
        if not errors:
            try:
                row = db.execute(
                    """INSERT INTO users (name, email, password_hash)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (form["name"], form["email"], generate_password_hash(password)),
                ).fetchone()
            except UniqueViolation:
                errors["email"] = "Этот email уже зарегистрирован."
            else:
                session.clear()
                session["user_id"] = row["id"]
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
            'SELECT id AS "ID", name AS "Name", password_hash AS "Password_hash" '
            'FROM users WHERE lower(email) = %s', (email,)
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
        f"{SELECT_REPORT} WHERE r.User_id = %s ORDER BY r.Created_at DESC, r.ID DESC",
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
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)

