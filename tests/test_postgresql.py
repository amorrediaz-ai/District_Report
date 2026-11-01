"""Run against TEST_DATABASE_URL. Every run uses its own temporary schema."""
import base64
import importlib
import io
import os
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII="
)


@pytest.fixture(scope="session")
def module():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a test PostgreSQL instance")
    schema = "district_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["options"] = "-csearch_path=" + schema
    scoped_url = urlunsplit(parts._replace(query=urlencode(query)))
    env = pytest.MonkeyPatch()
    env.setenv("DATABASE_URL", scoped_url)
    env.setenv("SECRET_KEY", "test-secret-key-only-for-isolated-tests-123456")
    env.setenv("ADMIN_EMAIL", "admin@example.test")
    env.setenv("ADMIN_PASSWORD", "test-admin-password-123")
    env.delenv("RENDER", raising=False)
    try:
        app_module = importlib.import_module("app")
        app_module.app.config["TESTING"] = True
        yield app_module
    finally:
        env.undo()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(autouse=True)
def clean(module):
    with module.connect_db() as conn:
        conn.execute("TRUNCATE report_like, report, users RESTART IDENTITY CASCADE")
    module.init_db()


def csrf(client):
    client.get("/")
    with client.session_transaction() as session:
        return session["csrf"]


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data})


def report(client, title="ЯМА на дороге", photo=False):
    data = dict(title=title, location="Астана", category="Дороги и тротуары",
                description="Нужно отремонтировать дорогу рядом с домом.")
    if photo:
        data["photo"] = (io.BytesIO(PNG), "picture.png")
    response = post(client, "/report/new", **data)
    assert response.status_code == 302
    return int(response.location.rsplit("/", 1)[1])


def admin_client(module):
    client = module.app.test_client()
    assert post(client, "/login", email="admin@example.test", password="test-admin-password-123").status_code == 302
    return client


def test_pages_and_assets(module):
    client = module.app.test_client()
    for path in ("/", "/login", "/register", "/how-it-works", "/report/new",
                 "/static/style.css", "/static/js/app.js", "/static/svg/logo.svg"):
        assert client.get(path).status_code == 200, path
    assert client.get("/missing-page").status_code == 404
    assert client.get("/my").status_code == 302


def test_register_login_and_my_reports(module):
    client = module.app.test_client()
    result = post(client, "/register", name="Житель", email="Citizen@Example.Test",
                  password="test-citizen-password", password2="test-citizen-password")
    assert result.status_code == 302
    rid = report(client)
    assert "ЯМА" in client.get("/my").get_data(as_text=True)
    with module.connect_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email='citizen@example.test'").fetchone()
        assert check_password_hash(user["password_hash"], "test-citizen-password")
        assert not user["is_admin"]
        assert conn.execute("SELECT user_id FROM report WHERE id=%s", (rid,)).fetchone()["user_id"] == user["id"]
    assert post(client, "/logout").status_code == 302
    assert post(client, "/login", email="citizen@example.test", password="wrong").status_code == 200
    assert post(client, "/login", email="citizen@example.test", password="test-citizen-password").status_code == 302
    duplicate = module.app.test_client()
    result = post(duplicate, "/register", name="Другой", email="CITIZEN@example.test",
                  password="test-citizen-password", password2="test-citizen-password")
    assert "уже зарегистрирован" in result.get_data(as_text=True)


def test_photo_and_report_survive_application_restart(module):
    client = module.app.test_client()
    rid = report(client, photo=True)
    with module.connect_db() as conn:
        row = conn.execute("SELECT photo, photo_data FROM report WHERE id=%s", (rid,)).fetchone()
        assert bytes(row["photo_data"]) == PNG
        filename = row["photo"]
    image = client.get("/uploads/" + filename)
    assert image.status_code == 200 and image.data == PNG
    assert image.mimetype == "image/png"
    assert client.get("/uploads/" + filename, headers={"If-None-Match": image.headers["ETag"]}).status_code == 304
    # New interpreter, new Flask app, same database; no local uploads directory.
    code = "import app; c=app.app.test_client(); assert c.get('/report/%d').status_code==200; assert c.get('/uploads/%s').data == %r" % (rid, filename, PNG)
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_search_filters_statistics_and_pagination(module):
    client = module.app.test_client()
    report(client, "ЯМА 100% на дороге")
    report(client, "ЯМА 1000 на дороге")
    report(client, "ЯМА A_B на дороге")
    report(client, "ЯМА ACB на дороге")
    report(client, "ЯМА A\\B на дороге")
    with module.app.test_request_context("/"):
        module.app.preprocess_request()
        for query, expected in (("яма", 5), ("100%", 1), ("A_B", 1), ("A\\B", 1), ("' OR 1=1 --", 0)):
            _, count, _, _ = module.fetch_reports(query, "", "", "new", 1)
            assert count == expected, query
        assert module.fetch_stats() == {"total": 5, "resolved": 0, "pending": 5, "in_progress": 0}
        assert module.fetch_reports("", "Освещение", "", "new", 1)[1] == 0
    for i in range(5):
        report(client, f"Обращение {i}")
    with module.app.test_request_context("/"):
        module.app.preprocess_request()
        rows, count, page, pages = module.fetch_reports("", "", "", "old", 999)
        assert (len(rows), count, page, pages) == (2, 10, 2, 2)


def test_likes_status_and_delete_cascade(module):
    client = module.app.test_client()
    rid = report(client, photo=True)
    url = f"/report/{rid}"
    assert post(client, url + "/like").json == {"liked": True, "likes": 1}
    assert post(client, url + "/like").json == {"liked": False, "likes": 0}
    assert post(client, url + "/like").json["likes"] == 1
    other = module.app.test_client()
    assert post(other, url + "/like").json["likes"] == 2
    assert post(client, url + "/status", status="Решено").status_code == 403
    assert post(client, url + "/delete").status_code == 403
    administrator = admin_client(module)
    assert post(administrator, url + "/status", status="Решено").status_code == 302
    with module.app.test_request_context("/"):
        module.app.preprocess_request()
        assert module.fetch_stats()["resolved"] == 1
    with module.connect_db() as conn:
        filename = conn.execute("SELECT photo FROM report WHERE id=%s", (rid,)).fetchone()["photo"]
    assert post(administrator, url + "/delete").status_code == 302
    assert client.get(url).status_code == 404
    assert client.get("/uploads/" + filename).status_code == 404
    with module.connect_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM report_like").fetchone()["n"] == 0
    assert post(client, url + "/like").status_code == 404


def test_validation_and_csrf_do_not_save_bad_data(module):
    client = module.app.test_client()
    assert client.post("/report/new", data={"title": "Bad"}).status_code == 400
    for payload in (b"<html>not an image</html>", b"x" * (5 * 1024 * 1024 + 1)):
        response = post(client, "/report/new", title="Проверка фото", location="Астана",
                        category="Другое", description="Описание тестового обращения",
                        photo=(io.BytesIO(payload), "image.png"))
        assert response.status_code == 200
    with module.connect_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM report").fetchone()["n"] == 0


def legacy_db(path, missing_photo=False):
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE Users (ID INTEGER, Name TEXT, Email TEXT, Password_hash TEXT, Is_admin INTEGER, Created_at TEXT);
            CREATE TABLE Report (ID INTEGER, Title TEXT, Location TEXT, Description TEXT, User_id INTEGER,
                counter_likes INTEGER, Status TEXT, type TEXT, Created_at TEXT, Photo TEXT);
            CREATE TABLE Report_like (Report_id INTEGER, Voter TEXT);
        ''')
        conn.execute("INSERT INTO Users VALUES (7, 'Житель', 'Old@Example.Test', ?, 1, '2026-10-01 12:00:00')",
                     (generate_password_hash("old-password-123"),))
        conn.execute("INSERT INTO Report VALUES (13, 'Старое обращение', 'Астана', 'Описание', 7, 2, 'На расмотрении', NULL, '2026-10-01 12:00:00', 'old.png')")
        conn.execute("INSERT INTO Report_like VALUES (13, 'u7')")
    if not missing_photo:
        (path.parent / "old.png").write_bytes(PNG)


def test_sqlite_migration_preserves_records_and_sequences(module, tmp_path):
    from migrate_sqlite import migrate
    source = tmp_path / "source.db"
    legacy_db(source)
    original = source.read_bytes()
    with module.connect_db() as conn:
        conn.execute("TRUNCATE report_like, report, users RESTART IDENTITY CASCADE")
    counts = migrate(source, tmp_path)
    assert counts == {"users": 1, "reports": 1, "likes": 1, "photos": 1, "skipped_photos": 0}
    assert source.read_bytes() == original
    module.init_db()
    client = module.app.test_client()
    assert post(client, "/login", email="old@example.test", password="old-password-123").status_code == 302
    assert client.get("/uploads/old.png").data == PNG
    assert report(client) > 13
    assert "Старое обращение" in client.get("/my").get_data(as_text=True)
    with pytest.raises(ValueError, match="уже содержит данные"):
        migrate(source, tmp_path)
    with module.connect_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM report").fetchone()["n"] == 2


def test_failed_migration_rolls_back_and_missing_photos_need_opt_in(module, tmp_path):
    from migrate_sqlite import migrate
    source = tmp_path / "source.db"
    legacy_db(source, missing_photo=True)
    with module.connect_db() as conn:
        conn.execute("TRUNCATE report_like, report, users RESTART IDENTITY CASCADE")
    with pytest.raises(ValueError, match="Не найдено фото"):
        migrate(source, tmp_path)
    with module.connect_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    assert migrate(source, tmp_path, skip_missing_photos=True)["skipped_photos"] == 1


def test_admin_password_change(module):
    runner = module.app.test_cli_runner()
    result = runner.invoke(args=["set-admin-password", "admin@example.test"],
                           input="new-admin-password-123\nnew-admin-password-123\n")
    assert result.exit_code == 0, result.output
    client = module.app.test_client()
    assert post(client, "/login", email="admin@example.test", password="new-admin-password-123").status_code == 302


def test_missing_configuration_fails_clearly(module, monkeypatch):
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        module.connect_db()


@pytest.mark.skipif(os.environ.get("TEST_DB_SERIAL") == "1", reason="Embedded test database has one connection")
def test_concurrent_votes_do_not_lose_updates(module):
    rid = report(module.app.test_client())
    url = f"/report/{rid}/like"
    clients = [module.app.test_client() for _ in range(6)]
    voters = [(client, csrf(client)) for client in clients]

    def vote(item):
        client, token = item
        response = client.post(url, data={"csrf_token": token})
        assert response.status_code == 200
        return response.json["likes"]

    with ThreadPoolExecutor(max_workers=6) as workers:
        assert sorted(workers.map(vote, voters)) == [1, 2, 3, 4, 5, 6]
    # Two concurrent clicks from the same session must toggle on then off.
    shared = module.app.test_client()
    token = csrf(shared)
    cookie = shared.get_cookie("session").value
    twins = [module.app.test_client(), module.app.test_client()]
    for client in twins:
        client.set_cookie("session", cookie)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(vote, [(client, token) for client in twins]))
    with module.connect_db() as conn:
        assert conn.execute("SELECT counter_likes FROM report WHERE id=%s", (rid,)).fetchone()["counter_likes"] == 6
        assert conn.execute("SELECT COUNT(*) AS n FROM report_like WHERE report_id=%s", (rid,)).fetchone()["n"] == 6
