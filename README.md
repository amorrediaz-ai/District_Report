# District Report

District Report is a website for reporting local problems in Astana. Residents can mark a location, attach photos and follow the administrator's response. A district administrator reviews reports and updates their status.

**Open the website:** [district-report.onrender.com](https://district-report.onrender.com/)

## Try the website

1. Open **Войти** and register with your email. Sign in if you want to edit your reports or receive updates. Guests can also submit a report.
2. Select **Сообщить о проблеме** (on a phone, **Сообщить**). Enter a title, category, address and description. You can place a pin on the map and attach up to five photos, up to 5 MB each.
3. Submit the form. The report page shows its status. Reports, photos, map pins and administrator replies are public.
4. Open **Мои обращения** to edit or delete your own reports. Open **Уведомления** to see updates. Administrators can open **Панель администратора** at `/admin` to change a status or reply.

## Run locally on Windows

You need **Python 3.12**, **PostgreSQL 16** and a clone or ZIP of this repository. If you use a ZIP, extract it first. Open PowerShell in the folder that contains `app.py`.

**1. Create the database.** In pgAdmin, connect to your local PostgreSQL server. Right-click **Databases → Create → Database**, name it `district_report`, and save. The commands below use the local PostgreSQL user `postgres` on port `5432`.

**2. Install the Python packages.**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**3. Set the local configuration.** Run this block in the same PowerShell window. The first password is for PostgreSQL. For the last prompt, choose a separate website administrator password of at least 12 characters.

```powershell
$dbPassword = Read-Host "Local PostgreSQL password" -AsSecureString
$dbValue = [System.Net.NetworkCredential]::new("", $dbPassword).Password
$env:DATABASE_URL = "postgresql://postgres:$([uri]::EscapeDataString($dbValue))@localhost:5432/district_report"
Remove-Variable dbPassword, dbValue

$env:SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
$env:ADMIN_EMAIL = Read-Host "Email for the website administrator"
$adminPassword = Read-Host "New website administrator password" -AsSecureString
$env:ADMIN_PASSWORD = [System.Net.NetworkCredential]::new("", $adminPassword).Password
Remove-Variable adminPassword
```

**4. Start the website.**

```powershell
.\.venv\Scripts\python.exe app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). On first launch, the app creates its tables and the first administrator. To check the setup, submit a local report with a photo, restart the app and reopen that report. Press **Ctrl+C** to stop the server.

PowerShell keeps these variables for the current window. To restart in that window, repeat step 4. In a new window, set the configuration again. A new `SECRET_KEY` signs users out, so keep a stable key for a shared deployment. The administrator credentials are used for setup only when the database has no administrator.

## Deploy on Render

1. Connect a Render **Web Service** to this GitHub repository. Select the **Python** runtime and the `main` branch. Use your existing Render PostgreSQL database, or create one in the same workspace and region as the web service.
2. Under **Environment**, add the four variables below. Keep the actual values in Render.

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | The database's **Internal Database URL** |
   | `SECRET_KEY` | A permanent random key. Generate it once with the Python command in step 3 above. |
   | `ADMIN_EMAIL` | Email for the first administrator |
   | `ADMIN_PASSWORD` | Initial administrator password, at least 12 characters |

3. Set the service commands.

   | Setting | Command |
   | --- | --- |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `gunicorn app:app --workers 1 --bind 0.0.0.0:$PORT` |

4. Deploy the service. Open its HTTPS address and check that the report list loads. Use `/login` to sign in and `/admin` to check administrator access.

[Render’s Flask deployment guide](https://render.com/docs/deploy-flask) · [Render PostgreSQL connections](https://render.com/docs/postgresql-creating-connecting)

## Where the data lives

The browser sends reports to a Python Flask app. Flask reads and writes PostgreSQL. The database stores accounts, password hashes, reports, coordinates, status history, notifications and the uploaded photo files. Restarting the web service keeps this data. Map pages load Leaflet and OpenStreetMap tiles over the internet.

For an **existing SQLite project**, use the [migration script](migrate_sqlite.py) only if you want to import old records. Back up the old database and uploads, and import into an empty PostgreSQL database **before starting the website**. Run `python migrate_sqlite.py --help` for the options.

## Run the server tests

In pgAdmin, create a separate local database named `district_report_test`. From the PowerShell window configured above, run:

```powershell
$env:TEST_DATABASE_URL = $env:DATABASE_URL -replace '/district_report$', '/district_report_test'
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q tests/test_postgresql.py
```

The [GitHub Actions workflow](.github/workflows/postgresql.yml) also runs browser tests at desktop and phone sizes.
