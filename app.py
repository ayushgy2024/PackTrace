from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import uuid
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
import requests as http_requests
from werkzeug.middleware.proxy_fix import ProxyFix
import zxingcpp

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local SQLite development does not require PostgreSQL.
    psycopg = None
    dict_row = None


BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's deployed application directory is read-only.  /tmp is writable, but
# it is intentionally ephemeral and is only a bridge until durable cloud
# database/object storage is configured.
DEFAULT_DATA_DIR = Path("/tmp/packtrace") if IS_VERCEL else BASE_DIR / "instance"
INSTANCE_DIR = Path(os.environ.get("PACKTRACE_DATA_DIR", DEFAULT_DATA_DIR))
VIDEO_DIR = INSTANCE_DIR / "private_evidence"
DATABASE = INSTANCE_DIR / "packtrace.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL.removeprefix("postgres://")

app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
app.config.update(
    MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
    SECRET_KEY=os.environ.get("PACKTRACE_SECRET_KEY", "local-development-only"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PACKTRACE_COOKIE_SECURE") == "1" or IS_VERCEL,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    AUTH_REQUIRED=os.environ.get("PACKTRACE_AUTH_REQUIRED", "1") != "0",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
oauth = OAuth(app)
google = None
google_drive = None
if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    google = oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )
    google_drive = oauth.register(
        name="google_drive",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email https://www.googleapis.com/auth/drive.file"
        },
    )
Image.MAX_IMAGE_PIXELS = 20_000_000


def allowed_google_emails() -> set[str]:
    return {
        email.strip().lower()
        for email in os.environ.get("PACKTRACE_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    }


def current_user() -> dict[str, str] | None:
    user = session.get("user")
    return user if isinstance(user, dict) else None


def local_auth_bypass() -> bool:
    hostname = request.host.split(":", 1)[0].lower()
    return os.environ.get("PACKTRACE_DEV_AUTH_BYPASS") == "1" and hostname in {
        "127.0.0.1",
        "localhost",
    }


def google_auth_configured() -> bool:
    secret_is_safe = bool(os.environ.get("PACKTRACE_SECRET_KEY")) or not IS_VERCEL
    return google is not None and secret_is_safe


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def require_google_login():
    if not app.config["AUTH_REQUIRED"] or app.config.get("TESTING") or local_auth_bypass():
        return None
    if request.endpoint in {"login", "google_login", "google_callback", "health", "static"}:
        return None
    if IS_VERCEL and not os.environ.get("PACKTRACE_SECRET_KEY"):
        if request.path.startswith("/api/"):
            return jsonify(error="Google authentication is not configured."), 503
        return redirect(url_for("login"))
    if current_user() is not None:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Google authentication is required."), 401
    if request.method == "GET":
        target = request.full_path.rstrip("?")
        if target.startswith("/") and not target.startswith("//"):
            session["post_login_next"] = target
    return redirect(url_for("login"))


@app.before_request
def verify_csrf_token():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or app.config.get("TESTING"):
        return None
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        if request.path.startswith("/api/"):
            return jsonify(error="The page security token expired. Refresh PackTrace and try again."), 400
        abort(400, "The page security token expired. Refresh PackTrace and try again.")
    return None


@app.context_processor
def authentication_context() -> dict[str, object]:
    user = current_user()
    if user is None and local_auth_bypass():
        user = {
            "sub": "local-development",
            "name": "Local developer",
            "email": "local@packtrace.test",
            "picture": "",
        }
    return {
        "current_user": user,
        "google_auth_configured": google_auth_configured(),
        "auth_required": app.config["AUTH_REQUIRED"],
        "csrf_token": csrf_token(),
    }


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/login")
def login():
    if current_user() is not None:
        return redirect(url_for("dashboard"))
    return render_template("login.html", auth_error="")


@app.get("/auth/google")
def google_login():
    if not google_auth_configured():
        return render_template(
            "login.html",
            auth_error="Google sign-in is not configured yet. Add the required environment variables.",
        ), 503
    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback():
    if not google_auth_configured():
        return redirect(url_for("login"))
    try:
        token = google.authorize_access_token()
        profile = token.get("userinfo") or google.userinfo(token=token)
    except OAuthError as error:
        app.logger.warning("Google authentication failed: %s", error.error)
        return render_template("login.html", auth_error="Google sign-in was cancelled or failed."), 400

    email = str(profile.get("email", "")).strip().lower()
    if not email or profile.get("email_verified") is not True:
        return render_template(
            "login.html", auth_error="Google did not provide a verified email address."
        ), 403
    allowed = allowed_google_emails()
    if allowed and email not in allowed:
        app.logger.warning("Google sign-in rejected for an email outside the allowlist")
        return render_template(
            "login.html", auth_error="This Google account is not allowed to access PackTrace."
        ), 403

    next_url = session.get("post_login_next", url_for("dashboard"))
    session.clear()
    session.permanent = True
    session["user"] = {
        "sub": str(profile.get("sub", "")),
        "email": email,
        "name": str(profile.get("name", email)),
        "picture": str(profile.get("picture", "")),
    }
    upsert_user(session["user"])
    safe_next = str(next_url)
    if not safe_next.startswith("/") or safe_next.startswith("//"):
        safe_next = url_for("dashboard")
    return redirect(safe_next)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


class ClosingSqliteConnection(sqlite3.Connection):
    """Commit or roll back a context block, then always release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PostgresConnection:
    """Small compatibility wrapper for the application's SQLite-style queries."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql: str, params=()):
        return self.connection.execute(sql.replace("?", "%s"), params)


@contextmanager
def get_db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("PostgreSQL support is unavailable. Install project requirements.")
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            yield PostgresConnection(connection)
        return
    connection = sqlite3.connect(DATABASE, factory=ClosingSqliteConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_URL:
        statements = (
            """CREATE TABLE IF NOT EXISTS users (
                   google_sub TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
                   picture TEXT NOT NULL DEFAULT '', created_at_utc TEXT NOT NULL,
                   last_login_at_utc TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evidence (
                   id TEXT PRIMARY KEY, owner_sub TEXT REFERENCES users(google_sub),
                   order_code TEXT NOT NULL, evidence_type TEXT NOT NULL,
                   recorded_at_utc TEXT NOT NULL, duration_seconds INTEGER NOT NULL DEFAULT 0,
                   size_bytes BIGINT NOT NULL, sha256 TEXT NOT NULL, mime_type TEXT NOT NULL,
                   local_filename TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
                   drive_file_id TEXT, drive_web_view_link TEXT, drive_md5 TEXT,
                   verified_at_utc TEXT, stop_reason TEXT NOT NULL DEFAULT '',
                   source_type TEXT NOT NULL DEFAULT 'web', latitude DOUBLE PRECISION,
                   longitude DOUBLE PRECISION, location_accuracy_m DOUBLE PRECISION,
                   location_recorded_at_utc TEXT, created_at_utc TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS recording_sessions (
                   id TEXT PRIMARY KEY, owner_sub TEXT REFERENCES users(google_sub),
                   order_code TEXT NOT NULL, evidence_type TEXT NOT NULL,
                   started_at_utc TEXT NOT NULL, stopped_at_utc TEXT,
                   stop_reason TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'RECORDING',
                   latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
                   location_accuracy_m DOUBLE PRECISION, location_recorded_at_utc TEXT,
                   evidence_id TEXT REFERENCES evidence(id))""",
            """CREATE TABLE IF NOT EXISTS drive_connections (
                   owner_sub TEXT PRIMARY KEY REFERENCES users(google_sub) ON DELETE CASCADE,
                   email TEXT NOT NULL, encrypted_refresh_token TEXT NOT NULL,
                   folder_id TEXT, connected_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS upload_sessions (
                   id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL REFERENCES users(google_sub) ON DELETE CASCADE,
                   order_code TEXT NOT NULL, evidence_type TEXT NOT NULL,
                   duration_seconds INTEGER NOT NULL, size_bytes BIGINT NOT NULL,
                   sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, recording_session_id TEXT,
                   stop_reason TEXT NOT NULL, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
                   location_accuracy_m DOUBLE PRECISION, location_recorded_at_utc TEXT,
                   state TEXT NOT NULL DEFAULT 'INITIATED', created_at_utc TEXT NOT NULL)""",
        )
        with get_db() as db:
            for statement in statements:
                db.execute(statement)
            # Make the same code safe when DATABASE_URL points to an older
            # PackTrace schema instead of a brand-new database.
            for statement in (
                "ALTER TABLE evidence ADD COLUMN IF NOT EXISTS owner_sub TEXT REFERENCES users(google_sub)",
                "ALTER TABLE evidence ADD COLUMN IF NOT EXISTS drive_web_view_link TEXT",
                "ALTER TABLE evidence ADD COLUMN IF NOT EXISTS drive_md5 TEXT",
                "ALTER TABLE recording_sessions ADD COLUMN IF NOT EXISTS owner_sub TEXT REFERENCES users(google_sub)",
            ):
                db.execute(statement)
            for statement in (
                "CREATE INDEX IF NOT EXISTS idx_evidence_owner_code ON evidence(owner_sub, order_code)",
                "CREATE INDEX IF NOT EXISTS idx_evidence_state ON evidence(state)",
                "CREATE INDEX IF NOT EXISTS idx_recording_owner_code ON recording_sessions(owner_sub, order_code, started_at_utc DESC)",
            ):
                db.execute(statement)
        return
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                owner_sub TEXT,
                order_code TEXT NOT NULL,
                evidence_type TEXT NOT NULL CHECK (evidence_type IN ('OUTBOUND', 'RTO')),
                recorded_at_utc TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                local_filename TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
                drive_file_id TEXT,
                drive_web_view_link TEXT,
                drive_md5 TEXT,
                verified_at_utc TEXT,
                stop_reason TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'web',
                latitude REAL,
                longitude REAL,
                location_accuracy_m REAL,
                location_recorded_at_utc TEXT,
                created_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_order_code
                ON evidence(order_code);
            CREATE INDEX IF NOT EXISTS idx_evidence_state
                ON evidence(state);
            CREATE TABLE IF NOT EXISTS recording_sessions (
                id TEXT PRIMARY KEY,
                owner_sub TEXT,
                order_code TEXT NOT NULL,
                evidence_type TEXT NOT NULL CHECK (evidence_type IN ('OUTBOUND', 'RTO')),
                started_at_utc TEXT NOT NULL,
                stopped_at_utc TEXT,
                stop_reason TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'RECORDING',
                latitude REAL,
                longitude REAL,
                location_accuracy_m REAL,
                location_recorded_at_utc TEXT,
                evidence_id TEXT REFERENCES evidence(id)
            );
            CREATE INDEX IF NOT EXISTS idx_recording_sessions_order_code
                ON recording_sessions(order_code, started_at_utc DESC);
            CREATE TABLE IF NOT EXISTS users (
                google_sub TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                picture TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                last_login_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drive_connections (
                owner_sub TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                encrypted_refresh_token TEXT NOT NULL,
                folder_id TEXT,
                connected_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upload_sessions (
                id TEXT PRIMARY KEY,
                owner_sub TEXT NOT NULL,
                order_code TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                recording_session_id TEXT,
                stop_reason TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                location_accuracy_m REAL,
                location_recorded_at_utc TEXT,
                state TEXT NOT NULL DEFAULT 'INITIATED',
                created_at_utc TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
        if "stop_reason" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''")
        if "source_type" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN source_type TEXT NOT NULL DEFAULT 'web'")
        for name, definition in (
            ("owner_sub", "TEXT"),
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("location_accuracy_m", "REAL"),
            ("location_recorded_at_utc", "TEXT"),
            ("drive_web_view_link", "TEXT"),
            ("drive_md5", "TEXT"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE evidence ADD COLUMN {name} {definition}")
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(recording_sessions)")}
        for name, definition in (
            ("owner_sub", "TEXT"),
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("location_accuracy_m", "REAL"),
            ("location_recorded_at_utc", "TEXT"),
        ):
            if name not in session_columns:
                db.execute(f"ALTER TABLE recording_sessions ADD COLUMN {name} {definition}")


def normalize_code(value: str) -> str:
    cleaned = "-".join(value.strip().upper().split())
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/."
    normalized = "".join(char for char in cleaned if char in allowed)
    if not 3 <= len(normalized) <= 64:
        raise ValueError("Order code must contain 3–64 supported characters.")
    return normalized


def parse_location(values) -> tuple[float | None, float | None, float | None, str | None]:
    latitude_raw = values.get("latitude")
    longitude_raw = values.get("longitude")
    if latitude_raw in (None, "") and longitude_raw in (None, ""):
        return None, None, None, None
    if latitude_raw in (None, "") or longitude_raw in (None, ""):
        raise ValueError("Latitude and longitude must be supplied together.")
    try:
        latitude = float(latitude_raw)
        longitude = float(longitude_raw)
        accuracy_raw = values.get("location_accuracy_m")
        accuracy = float(accuracy_raw) if accuracy_raw not in (None, "") else None
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid geolocation values.") from error
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Geolocation coordinates are outside valid bounds.")
    if accuracy is not None and (accuracy < 0 or accuracy > 100_000):
        raise ValueError("Geolocation accuracy is outside valid bounds.")
    recorded_at = str(values.get("location_recorded_at_utc") or "").strip()
    if recorded_at:
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid geolocation timestamp.") from error
    return latitude, longitude, accuracy, recorded_at or None


def owner_sub() -> str:
    user = current_user()
    if user and user.get("sub"):
        return str(user["sub"])
    if local_auth_bypass():
        return "local-development"
    return ""


def upsert_user(user: dict[str, str]) -> None:
    subject = str(user.get("sub", "")).strip()
    if not subject:
        return
    timestamp = utc_now()
    with get_db() as db:
        db.execute(
            """INSERT INTO users
               (google_sub, email, name, picture, created_at_utc, last_login_at_utc)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(google_sub) DO UPDATE SET
                   email = excluded.email,
                   name = excluded.name,
                   picture = excluded.picture,
                   last_login_at_utc = excluded.last_login_at_utc""",
            (
                subject,
                str(user.get("email", "")),
                str(user.get("name", "")),
                str(user.get("picture", "")),
                timestamp,
                timestamp,
            ),
        )


def token_cipher() -> Fernet | None:
    secret = os.environ.get("PACKTRACE_TOKEN_ENCRYPTION_KEY", "").strip()
    if not secret:
        return None
    key = urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_refresh_token(value: str) -> str:
    cipher = token_cipher()
    if cipher is None:
        raise RuntimeError("PACKTRACE_TOKEN_ENCRYPTION_KEY is not configured.")
    return cipher.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_refresh_token(value: str) -> str:
    cipher = token_cipher()
    if cipher is None:
        raise RuntimeError("PACKTRACE_TOKEN_ENCRYPTION_KEY is not configured.")
    try:
        return cipher.decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        raise RuntimeError("The saved Google Drive connection cannot be decrypted.") from error


def drive_connection(subject: str | None = None):
    subject = subject or owner_sub()
    if not subject:
        return None
    with get_db() as db:
        return db.execute(
            "SELECT * FROM drive_connections WHERE owner_sub = ?", (subject,)
        ).fetchone()


def drive_ready() -> bool:
    return bool(
        google_drive is not None
        and token_cipher() is not None
        and (not IS_VERCEL or DATABASE_URL)
        and drive_connection() is not None
    )


def refresh_drive_access_token(connection) -> str:
    refresh_token = decrypt_refresh_token(connection["encrypted_refresh_token"])
    response = http_requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if not response.ok:
        app.logger.warning("Google Drive token refresh failed with status %s", response.status_code)
        raise RuntimeError("Google Drive authorization expired. Reconnect Drive in Settings.")
    access_token = str(response.json().get("access_token", ""))
    if not access_token:
        raise RuntimeError("Google Drive did not return an access token.")
    return access_token


def drive_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def ensure_drive_folder(access_token: str, connection) -> str:
    folder_id = str(connection["folder_id"] or "")
    if folder_id:
        return folder_id
    response = http_requests.post(
        "https://www.googleapis.com/drive/v3/files",
        params={"fields": "id"},
        headers={**drive_headers(access_token), "Content-Type": "application/json"},
        json={"name": "PackTrace Evidence", "mimeType": "application/vnd.google-apps.folder"},
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError("PackTrace could not create its folder in Google Drive.")
    folder_id = str(response.json().get("id", ""))
    if not folder_id:
        raise RuntimeError("Google Drive did not return the evidence folder ID.")
    with get_db() as db:
        db.execute(
            "UPDATE drive_connections SET folder_id = ?, updated_at_utc = ? WHERE owner_sub = ?",
            (folder_id, utc_now(), owner_sub()),
        )
    return folder_id


def evidence_rows(query: str = "", limit: int | None = None) -> list:
    sql = """SELECT evidence.*,
                    COUNT(*) OVER (PARTITION BY owner_sub, order_code) AS duplicate_count
             FROM evidence"""
    params: list[object] = []
    conditions = []
    subject = owner_sub()
    if subject:
        conditions.append("owner_sub = ?")
        params.append(subject)
    if query:
        conditions.append("order_code LIKE ?")
        params.append(f"%{query}%")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY recorded_at_utc DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db() as db:
        return list(db.execute(sql, params).fetchall())


@app.template_filter("filesize")
def filesize(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@app.template_filter("duration")
def duration(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


@app.template_filter("friendly_time")
def friendly_time(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone().strftime("%d %b %Y, %I:%M %p")


@app.context_processor
def shared_context() -> dict[str, object]:
    subject = owner_sub()
    where = " WHERE owner_sub = ?" if subject else ""
    params = (subject,) if subject else ()
    with get_db() as db:
        totals = db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN state = 'VERIFIED' THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN state != 'VERIFIED' THEN 1 ELSE 0 END) AS pending,
                      COALESCE(SUM(CASE WHEN state != 'VERIFIED' THEN size_bytes ELSE 0 END), 0) AS local_bytes
               FROM evidence""" + where,
            params,
        ).fetchone()
    try:
        connection = drive_connection(subject) if subject else None
    except Exception:
        app.logger.exception("Drive connection status could not be loaded")
        connection = None
    return {
        "totals": totals,
        "drive_connected": connection is not None,
        "drive_connection": connection,
        "persistent_database": bool(DATABASE_URL),
    }


@app.get("/")
def dashboard():
    return render_template("dashboard.html", active_page="home", recent=evidence_rows(limit=5))


@app.get("/evidence")
def evidence_library():
    query = request.args.get("q", "").strip().upper()
    saved = request.args.get("saved", "").strip().upper()
    try:
        duplicate_count = max(0, int(request.args.get("duplicates", "0")))
    except ValueError:
        duplicate_count = 0
    return render_template(
        "evidence.html",
        active_page="evidence",
        evidence=evidence_rows(query=query),
        query=query,
        saved=saved,
        saved_to_drive=request.args.get("storage") == "drive",
        duplicate_count=duplicate_count,
    )


@app.get("/uploads")
def uploads():
    subject = owner_sub()
    with get_db() as db:
        pending = list(
            db.execute(
                "SELECT * FROM evidence WHERE state != 'VERIFIED' AND owner_sub = ? ORDER BY created_at_utc DESC",
                (subject,),
            )
        )
    return render_template("uploads.html", active_page="uploads", pending=pending)


@app.get("/settings")
def settings():
    return render_template(
        "settings.html",
        active_page="settings",
        drive_error=request.args.get("drive_error", ""),
        drive_saved=request.args.get("drive_saved") == "1",
    )


@app.get("/drive/connect")
def connect_google_drive():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if google_drive is None or token_cipher() is None:
        return redirect(
            url_for(
                "settings",
                drive_error="Google Drive needs OAuth credentials and PACKTRACE_TOKEN_ENCRYPTION_KEY.",
            )
        )
    if IS_VERCEL and not DATABASE_URL:
        return redirect(
            url_for(
                "settings",
                drive_error="Configure DATABASE_URL before connecting Drive so authorization survives Vercel restarts.",
            )
        )
    redirect_uri = url_for("google_drive_callback", _external=True)
    return google_drive.authorize_redirect(
        redirect_uri,
        access_type="offline",
        prompt="consent select_account",
        include_granted_scopes="true",
        login_hint=user.get("email", ""),
    )


@app.get("/auth/google/drive/callback")
def google_drive_callback():
    user = current_user()
    if not user or google_drive is None:
        return redirect(url_for("login"))
    try:
        token = google_drive.authorize_access_token()
        profile = token.get("userinfo") or google_drive.userinfo(token=token)
        if str(profile.get("sub", "")) != str(user.get("sub", "")):
            return redirect(
                url_for("settings", drive_error="Connect the same Google account used to sign in.")
            )
        # A browser session can survive a deployment that moves PackTrace from
        # local SQLite to PostgreSQL. Synchronize it before inserting records
        # whose owner_sub has a foreign key to the durable users table.
        upsert_user(user)
        refresh_token = str(token.get("refresh_token", ""))
        if not refresh_token:
            return redirect(
                url_for("settings", drive_error="Google did not provide offline Drive access. Try connecting again.")
            )
        encrypted_token = encrypt_refresh_token(refresh_token)
        timestamp = utc_now()
        with get_db() as db:
            db.execute(
                """INSERT INTO drive_connections
                   (owner_sub, email, encrypted_refresh_token, folder_id, connected_at_utc, updated_at_utc)
                   VALUES (?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(owner_sub) DO UPDATE SET
                       email = excluded.email,
                       encrypted_refresh_token = excluded.encrypted_refresh_token,
                       folder_id = NULL,
                       updated_at_utc = excluded.updated_at_utc""",
                (owner_sub(), str(profile.get("email", "")), encrypted_token, timestamp, timestamp),
            )
        connection = drive_connection()
        access_token = str(token.get("access_token", "")) or refresh_drive_access_token(connection)
        ensure_drive_folder(access_token, connection)
    except OAuthError as error:
        app.logger.warning("Google Drive authorization failed: %s", error.error)
        return redirect(url_for("settings", drive_error="Google Drive authorization was cancelled or failed."))
    except Exception:
        app.logger.exception("Google Drive connection failed")
        return redirect(url_for("settings", drive_error="Google Drive could not be connected safely."))
    return redirect(url_for("settings", drive_saved="1"))


@app.post("/drive/disconnect")
def disconnect_google_drive():
    connection = drive_connection()
    if connection:
        try:
            refresh_token = decrypt_refresh_token(connection["encrypted_refresh_token"])
            http_requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": refresh_token},
                timeout=15,
            )
        except Exception:
            app.logger.warning("Drive token revocation could not be confirmed")
        with get_db() as db:
            db.execute("DELETE FROM drive_connections WHERE owner_sub = ?", (owner_sub(),))
    return redirect(url_for("settings"))


@app.post("/api/drive/uploads/initiate")
def initiate_drive_upload():
    connection = drive_connection()
    if connection is None:
        return jsonify(error="Connect Google Drive in Settings before uploading."), 409
    data = request.get_json(silent=True) or {}
    try:
        upload_id = str(uuid.UUID(str(data.get("upload_id", ""))))
        order_code = normalize_code(str(data.get("order_code", "")))
        evidence_type = str(data.get("evidence_type", "")).upper()
        if evidence_type not in {"OUTBOUND", "RTO"}:
            raise ValueError("Evidence type must be OUTBOUND or RTO.")
        duration_seconds = max(0, int(data.get("duration_seconds", 0)))
        size_bytes = int(data.get("size_bytes", 0))
        if size_bytes <= 0 or size_bytes > 5 * 1024 * 1024 * 1024:
            raise ValueError("Recording size is outside the supported range.")
        mime_type = str(data.get("mime_type", "video/webm"))[:100]
        if not mime_type.startswith("video/"):
            raise ValueError("The upload must be a video recording.")
        sha256 = str(data.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("A valid SHA-256 recording hash is required.")
        latitude, longitude, accuracy, location_recorded_at = parse_location(data)
        recording_session_id = str(data.get("recording_session_id", "")).strip()
        if recording_session_id:
            recording_session_id = str(uuid.UUID(recording_session_id))
    except (TypeError, ValueError) as error:
        return jsonify(error=str(error)), 400

    with get_db() as db:
        existing = db.execute(
            "SELECT owner_sub, state FROM upload_sessions WHERE id = ?", (upload_id,)
        ).fetchone()
    if existing is not None and existing["owner_sub"] != owner_sub():
        return jsonify(error="This upload ID is already in use."), 409
    if existing is not None and existing["state"] == "COMPLETED":
        return jsonify(error="This recording upload is already complete."), 409

    try:
        access_token = refresh_drive_access_token(connection)
        folder_id = ensure_drive_folder(access_token, connection)
        extension = "webm" if "webm" in mime_type else "mp4"
        filename_code = re.sub(r"[^A-Z0-9._-]", "-", order_code)[:64]
        filename = f"{filename_code}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{upload_id[:8]}.{extension}"
        drive_response = http_requests.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "resumable", "fields": "id,name,size,md5Checksum,webViewLink,parents"},
            headers={
                **drive_headers(access_token),
                # The returned session is used directly by this web origin.
                # Supplying it here lets Google authorize the later browser PUTs.
                "Origin": request.host_url.rstrip("/"),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size_bytes),
            },
            json={
                "name": filename,
                "parents": [folder_id],
                "description": f"PackTrace {evidence_type} evidence for {order_code}",
            },
            timeout=25,
        )
        upload_url = drive_response.headers.get("Location", "")
        if not drive_response.ok or not upload_url:
            app.logger.warning("Drive resumable session failed with status %s", drive_response.status_code)
            return jsonify(error="Google Drive could not start the video upload."), 502
    except RuntimeError as error:
        return jsonify(error=str(error)), 409
    except http_requests.RequestException:
        return jsonify(error="Google Drive is temporarily unavailable."), 502

    stop_reason = str(data.get("stop_reason", "MANUAL")).strip().upper()[:40] or "MANUAL"
    with get_db() as db:
        db.execute(
            """INSERT INTO upload_sessions
               (id, owner_sub, order_code, evidence_type, duration_seconds, size_bytes,
                sha256, mime_type, recording_session_id, stop_reason, latitude, longitude,
                location_accuracy_m, location_recorded_at_utc, state, created_at_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INITIATED', ?)
               ON CONFLICT(id) DO UPDATE SET state = 'INITIATED', created_at_utc = excluded.created_at_utc
               WHERE upload_sessions.owner_sub = excluded.owner_sub""",
            (
                upload_id, owner_sub(), order_code, evidence_type, duration_seconds,
                size_bytes, sha256, mime_type, recording_session_id or None, stop_reason,
                latitude, longitude, accuracy, location_recorded_at, utc_now(),
            ),
        )
    return jsonify(upload_id=upload_id, upload_url=upload_url, filename=filename)


@app.post("/api/drive/uploads/complete")
def complete_drive_upload():
    data = request.get_json(silent=True) or {}
    try:
        upload_id = str(uuid.UUID(str(data.get("upload_id", ""))))
    except ValueError:
        return jsonify(error="Invalid upload session."), 400
    drive_file_id = str(data.get("drive_file_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", drive_file_id):
        return jsonify(error="Google Drive did not return a valid file ID."), 400
    with get_db() as db:
        pending = db.execute(
            "SELECT * FROM upload_sessions WHERE id = ? AND owner_sub = ?",
            (upload_id, owner_sub()),
        ).fetchone()
    if pending is None:
        return jsonify(error="Upload session was not found."), 404
    if pending["state"] == "COMPLETED":
        with get_db() as db:
            existing_evidence = db.execute(
                "SELECT order_code FROM evidence WHERE id = ? AND owner_sub = ?",
                (upload_id, owner_sub()),
            ).fetchone()
            duplicate_count = db.execute(
                "SELECT COUNT(*) AS count FROM evidence WHERE owner_sub = ? AND order_code = ?",
                (owner_sub(), pending["order_code"]),
            ).fetchone()["count"]
        if existing_evidence is not None:
            return jsonify(
                id=upload_id,
                duplicate_count=duplicate_count,
                redirect=url_for(
                    "evidence_library",
                    saved=pending["order_code"],
                    storage="drive",
                    duplicates=duplicate_count if duplicate_count > 1 else None,
                ),
            )
    connection = drive_connection()
    if connection is None:
        return jsonify(error="Reconnect Google Drive to verify this upload."), 409
    try:
        access_token = refresh_drive_access_token(connection)
        response = http_requests.get(
            f"https://www.googleapis.com/drive/v3/files/{drive_file_id}",
            params={"fields": "id,name,size,md5Checksum,webViewLink,parents"},
            headers=drive_headers(access_token),
            timeout=20,
        )
        if not response.ok:
            return jsonify(error="The uploaded Drive file could not be verified."), 502
        drive_file = response.json()
        if int(drive_file.get("size", -1)) != int(pending["size_bytes"]):
            return jsonify(error="The uploaded Drive file size did not match the recording."), 409
        folder_id = str(connection["folder_id"] or "")
        if folder_id and folder_id not in drive_file.get("parents", []):
            return jsonify(error="The uploaded file is outside the PackTrace Drive folder."), 409
    except (ValueError, RuntimeError, http_requests.RequestException) as error:
        app.logger.warning("Drive upload verification failed: %s", type(error).__name__)
        return jsonify(error="The uploaded Drive file could not be verified safely."), 502

    timestamp = utc_now()
    evidence_id = upload_id
    with get_db() as db:
        duplicate_count = db.execute(
            "SELECT COUNT(*) AS count FROM evidence WHERE owner_sub = ? AND order_code = ?",
            (owner_sub(), pending["order_code"]),
        ).fetchone()["count"]
        db.execute(
            """INSERT INTO evidence
               (id, owner_sub, order_code, evidence_type, recorded_at_utc, duration_seconds,
                size_bytes, sha256, mime_type, local_filename, state, drive_file_id,
                drive_web_view_link, drive_md5, verified_at_utc, stop_reason, source_type,
                latitude, longitude, location_accuracy_m, location_recorded_at_utc, created_at_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'VERIFIED', ?, ?, ?, ?, ?,
                       'google_drive', ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (
                evidence_id, owner_sub(), pending["order_code"], pending["evidence_type"],
                timestamp, pending["duration_seconds"], pending["size_bytes"], pending["sha256"],
                pending["mime_type"], f"drive:{drive_file_id}", drive_file_id,
                str(drive_file.get("webViewLink", "")), str(drive_file.get("md5Checksum", "")),
                timestamp, pending["stop_reason"], pending["latitude"], pending["longitude"],
                pending["location_accuracy_m"], pending["location_recorded_at_utc"], timestamp,
            ),
        )
        if pending["recording_session_id"]:
            db.execute(
                """UPDATE recording_sessions SET stopped_at_utc = ?, stop_reason = ?,
                   state = 'SAVED', evidence_id = ? WHERE id = ? AND owner_sub = ?""",
                (
                    timestamp, pending["stop_reason"], evidence_id,
                    pending["recording_session_id"], owner_sub(),
                ),
            )
        db.execute("UPDATE upload_sessions SET state = 'COMPLETED' WHERE id = ?", (upload_id,))
    duplicate_count += 1
    return jsonify(
        id=evidence_id,
        duplicate_count=duplicate_count,
        redirect=url_for(
            "evidence_library",
            saved=pending["order_code"],
            storage="drive",
            duplicates=duplicate_count if duplicate_count > 1 else None,
        ),
    )


@app.post("/api/evidence")
def create_evidence():
    video = request.files.get("video")
    if video is None or not video.filename:
        return jsonify(error="A recording is required."), 400
    try:
        order_code = normalize_code(request.form.get("order_code", ""))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    evidence_type = request.form.get("evidence_type", "").upper()
    if evidence_type not in {"OUTBOUND", "RTO"}:
        return jsonify(error="Evidence type must be OUTBOUND or RTO."), 400
    try:
        duration_seconds = max(0, int(request.form.get("duration_seconds", "0")))
    except ValueError:
        return jsonify(error="Invalid recording duration."), 400
    try:
        latitude, longitude, location_accuracy_m, location_recorded_at_utc = parse_location(request.form)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    stop_reason = request.form.get("stop_reason", "MANUAL").strip().upper()[:40] or "MANUAL"
    recording_session_id = request.form.get("recording_session_id", "").strip()
    if recording_session_id:
        try:
            recording_session_id = str(uuid.UUID(recording_session_id))
        except ValueError:
            return jsonify(error="Invalid recording session."), 400

    evidence_id = str(uuid.uuid4())
    extension = ".webm" if "webm" in (video.mimetype or "") else ".mp4"
    filename = f"{evidence_id}{extension}"
    destination = VIDEO_DIR / filename
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with destination.open("wb") as output:
            while chunk := video.stream.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
        if size_bytes == 0:
            destination.unlink(missing_ok=True)
            return jsonify(error="The recording was empty."), 400
        timestamp = utc_now()
        with get_db() as db:
            duplicate_count = db.execute(
                "SELECT COUNT(*) AS count FROM evidence WHERE owner_sub = ? AND order_code = ?",
                (owner_sub(), order_code),
            ).fetchone()["count"]
            db.execute(
                """INSERT INTO evidence
                   (id, owner_sub, order_code, evidence_type, recorded_at_utc, duration_seconds,
                    size_bytes, sha256, mime_type, local_filename, state, stop_reason,
                    source_type, latitude, longitude, location_accuracy_m,
                    location_recorded_at_utc, created_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD', ?, 'web',
                           ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    owner_sub(),
                    order_code,
                    evidence_type,
                    timestamp,
                    duration_seconds,
                    size_bytes,
                    digest.hexdigest(),
                    video.mimetype or "application/octet-stream",
                    filename,
                    stop_reason,
                    latitude,
                    longitude,
                    location_accuracy_m,
                    location_recorded_at_utc,
                    timestamp,
                ),
            )
            if recording_session_id:
                db.execute(
                    """INSERT INTO recording_sessions
                       (id, owner_sub, order_code, evidence_type, started_at_utc, stopped_at_utc,
                        stop_reason, state, evidence_id, latitude, longitude,
                        location_accuracy_m, location_recorded_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'SAVED', ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           stopped_at_utc = excluded.stopped_at_utc,
                           stop_reason = excluded.stop_reason,
                           state = 'SAVED',
                           evidence_id = excluded.evidence_id,
                           latitude = COALESCE(excluded.latitude, recording_sessions.latitude),
                           longitude = COALESCE(excluded.longitude, recording_sessions.longitude),
                           location_accuracy_m = COALESCE(excluded.location_accuracy_m, recording_sessions.location_accuracy_m),
                           location_recorded_at_utc = COALESCE(excluded.location_recorded_at_utc, recording_sessions.location_recorded_at_utc)""",
                    (
                        recording_session_id,
                        owner_sub(),
                        order_code,
                        evidence_type,
                        timestamp,
                        timestamp,
                        stop_reason,
                        evidence_id,
                        latitude,
                        longitude,
                        location_accuracy_m,
                        location_recorded_at_utc,
                    ),
                )
    except Exception:
        destination.unlink(missing_ok=True)
        app.logger.exception("Evidence storage failed without logging order data")
        return jsonify(error="The recording could not be stored safely."), 500
    duplicate_count += 1
    return jsonify(
        id=evidence_id,
        duplicate_count=duplicate_count,
        redirect=url_for(
            "evidence_library",
            saved=order_code,
            duplicates=duplicate_count if duplicate_count > 1 else None,
        ),
    ), 201


@app.post("/api/recording-sessions")
def start_recording_session():
    data = request.get_json(silent=True) or {}
    try:
        session_id = str(uuid.UUID(str(data.get("id", ""))))
        order_code = normalize_code(str(data.get("order_code", "")))
    except (ValueError, TypeError) as error:
        return jsonify(error=str(error) or "Invalid recording session."), 400
    evidence_type = str(data.get("evidence_type", "")).upper()
    if evidence_type not in {"OUTBOUND", "RTO"}:
        return jsonify(error="Evidence type must be OUTBOUND or RTO."), 400
    try:
        latitude, longitude, location_accuracy_m, location_recorded_at_utc = parse_location(data)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    with get_db() as db:
        insert_prefix = "INSERT INTO" if DATABASE_URL else "INSERT OR IGNORE INTO"
        conflict_suffix = " ON CONFLICT(id) DO NOTHING" if DATABASE_URL else ""
        db.execute(
            f"""{insert_prefix} recording_sessions
               (id, owner_sub, order_code, evidence_type, started_at_utc, state, latitude,
                longitude, location_accuracy_m, location_recorded_at_utc)
               VALUES (?, ?, ?, ?, ?, 'RECORDING', ?, ?, ?, ?){conflict_suffix}""",
            (
                session_id,
                owner_sub(),
                order_code,
                evidence_type,
                utc_now(),
                latitude,
                longitude,
                location_accuracy_m,
                location_recorded_at_utc,
            ),
        )
    return jsonify(id=session_id, state="RECORDING"), 201


@app.post("/api/scan")
def scan_shipping_label():
    """Decode QR and 1D/2D shipping barcodes from a camera-frame JPEG."""
    max_frame_bytes = 10 * 1024 * 1024
    frame = request.files.get("frame")
    if frame is None:
        return jsonify(error="A camera frame is required."), 400
    if request.content_length and request.content_length > max_frame_bytes:
        return jsonify(error="Camera frame is too large."), 413
    try:
        raw = frame.stream.read(max_frame_bytes + 1)
        if not raw or len(raw) > max_frame_bytes:
            return jsonify(error="Camera frame is empty or too large."), 400
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            # Preserve narrow 1D bars. Reducing phone photos to 1280px made many
            # courier labels undecodable even though QR test images still passed.
            image.thumbnail((2600, 2600), Image.Resampling.LANCZOS)
            gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
            enhanced = ImageEnhance.Sharpness(ImageEnhance.Contrast(gray).enhance(1.65)).enhance(1.4)
            candidates = [image, gray, enhanced]
            if max(image.size) < 1500:
                candidates.append(enhanced.resize((enhanced.width * 2, enhanced.height * 2), Image.Resampling.LANCZOS))

            results = []
            for candidate in candidates:
                results = zxingcpp.read_barcodes(
                    candidate,
                    try_rotate=True,
                    try_downscale=True,
                    try_invert=True,
                    return_errors=False,
                )
                if results:
                    break
    except (UnidentifiedImageError, OSError, ValueError):
        return jsonify(error="Camera frame is not a valid image."), 400
    except Exception:
        app.logger.exception("Barcode decoding failed without logging frame contents")
        return jsonify(error="Barcode decoder failed safely."), 500

    codes = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        text = result.text.strip()
        barcode_format = str(result.format)
        key = (text, barcode_format)
        if text and key not in seen:
            seen.add(key)
            codes.append({"text": text, "format": barcode_format})
    return jsonify(codes=codes)


@app.get("/evidence/<evidence_id>/video")
def play_evidence(evidence_id: str):
    with get_db() as db:
        item = db.execute(
            "SELECT * FROM evidence WHERE id = ? AND owner_sub = ?",
            (evidence_id, owner_sub()),
        ).fetchone()
    if item is None:
        abort(404)
    if item["drive_file_id"] and item["drive_web_view_link"]:
        return redirect(item["drive_web_view_link"])
    path = VIDEO_DIR / item["local_filename"]
    if not path.is_file() or path.parent != VIDEO_DIR:
        abort(404)
    return send_file(path, mimetype=item["mime_type"], conditional=True)


@app.post("/evidence/<evidence_id>/remove")
def remove_unverified(evidence_id: str):
    # Deliberately blocked: the UI cannot delete locally retained evidence.
    abort(403, "Unverified evidence cannot be deleted.")


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="Recording exceeds the 1 GB local limit."), 413


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
