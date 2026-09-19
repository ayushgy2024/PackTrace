from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix
import zxingcpp


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
VIDEO_DIR = INSTANCE_DIR / "private_evidence"
DATABASE = INSTANCE_DIR / "packtrace.sqlite3"

app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
if os.environ.get("VERCEL") and not os.environ.get("PACKTRACE_SECRET_KEY"):
    raise RuntimeError("PACKTRACE_SECRET_KEY must be configured in Vercel.")
app.config.update(
    MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
    SECRET_KEY=os.environ.get("PACKTRACE_SECRET_KEY", "local-development-only"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PACKTRACE_COOKIE_SECURE") == "1"
    or bool(os.environ.get("VERCEL")),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
oauth = OAuth(app)
google = None
if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    google = oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
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


@app.before_request
def require_google_login():
    if app.config.get("TESTING") or local_auth_bypass():
        return None
    if request.endpoint in {"login", "google_login", "google_callback", "health", "static"}:
        return None
    if current_user() is not None:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Google authentication is required."), 401
    if request.method == "GET":
        target = request.full_path.rstrip("?")
        if target.startswith("/") and not target.startswith("//"):
            session["post_login_next"] = target
    return redirect(url_for("login"))


@app.context_processor
def authentication_context() -> dict[str, object]:
    user = current_user()
    if user is None and local_auth_bypass():
        user = {"name": "Local developer", "email": "local@packtrace.test", "picture": ""}
    return {"current_user": user, "google_auth_configured": google is not None}


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
    if google is None:
        return render_template(
            "login.html",
            auth_error="Google authentication is not configured yet. Add the client ID and secret.",
        ), 503
    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback():
    if google is None:
        return redirect(url_for("login"))
    try:
        token = google.authorize_access_token()
        profile = token.get("userinfo") or google.userinfo(token=token)
    except OAuthError as error:
        app.logger.warning("Google authentication failed: %s", error.error)
        return render_template("login.html", auth_error="Google sign-in was cancelled or failed."), 400

    email = str(profile.get("email", "")).strip().lower()
    if not email or profile.get("email_verified") is not True:
        return render_template("login.html", auth_error="Google did not provide a verified email address."), 403
    allowed = allowed_google_emails()
    if allowed and email not in allowed:
        app.logger.warning("Google sign-in rejected for an email outside the allowlist")
        return render_template("login.html", auth_error="This Google account is not allowed to access PackTrace."), 403

    next_url = session.get("post_login_next", url_for("dashboard"))
    session.clear()
    session.permanent = True
    session["user"] = {
        "sub": str(profile.get("sub", "")),
        "email": email,
        "name": str(profile.get("name", email)),
        "picture": str(profile.get("picture", "")),
    }
    return redirect(next_url if str(next_url).startswith("/") and not str(next_url).startswith("//") else url_for("dashboard"))


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


def get_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, factory=ClosingSqliteConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
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
                verified_at_utc TEXT,
                stop_reason TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'web',
                created_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_order_code
                ON evidence(order_code);
            CREATE INDEX IF NOT EXISTS idx_evidence_state
                ON evidence(state);
            CREATE TABLE IF NOT EXISTS recording_sessions (
                id TEXT PRIMARY KEY,
                order_code TEXT NOT NULL,
                evidence_type TEXT NOT NULL CHECK (evidence_type IN ('OUTBOUND', 'RTO')),
                started_at_utc TEXT NOT NULL,
                stopped_at_utc TEXT,
                stop_reason TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'RECORDING',
                evidence_id TEXT REFERENCES evidence(id)
            );
            CREATE INDEX IF NOT EXISTS idx_recording_sessions_order_code
                ON recording_sessions(order_code, started_at_utc DESC);
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
        if "stop_reason" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''")
        if "source_type" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN source_type TEXT NOT NULL DEFAULT 'web'")


def normalize_code(value: str) -> str:
    cleaned = "-".join(value.strip().upper().split())
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/."
    normalized = "".join(char for char in cleaned if char in allowed)
    if not 3 <= len(normalized) <= 64:
        raise ValueError("Order code must contain 3–64 supported characters.")
    return normalized


def evidence_rows(query: str = "", limit: int | None = None) -> list[sqlite3.Row]:
    sql = """SELECT evidence.*,
                    COUNT(*) OVER (PARTITION BY order_code) AS duplicate_count
             FROM evidence"""
    params: list[object] = []
    if query:
        sql += " WHERE order_code LIKE ?"
        params.append(f"%{query}%")
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
    with get_db() as db:
        totals = db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN state = 'VERIFIED' THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN state != 'VERIFIED' THEN 1 ELSE 0 END) AS pending,
                      COALESCE(SUM(CASE WHEN state != 'VERIFIED' THEN size_bytes ELSE 0 END), 0) AS local_bytes
               FROM evidence"""
        ).fetchone()
    return {"totals": totals}


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
        duplicate_count=duplicate_count,
    )


@app.get("/uploads")
def uploads():
    with get_db() as db:
        pending = list(
            db.execute("SELECT * FROM evidence WHERE state != 'VERIFIED' ORDER BY created_at_utc DESC")
        )
    return render_template("uploads.html", active_page="uploads", pending=pending)


@app.get("/settings")
def settings():
    return render_template("settings.html", active_page="settings")


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
                "SELECT COUNT(*) FROM evidence WHERE order_code = ?", (order_code,)
            ).fetchone()[0]
            db.execute(
                """INSERT INTO evidence
                   (id, order_code, evidence_type, recorded_at_utc, duration_seconds,
                    size_bytes, sha256, mime_type, local_filename, state, stop_reason,
                    source_type, created_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD', ?, 'web', ?)""",
                (
                    evidence_id,
                    order_code,
                    evidence_type,
                    timestamp,
                    duration_seconds,
                    size_bytes,
                    digest.hexdigest(),
                    video.mimetype or "application/octet-stream",
                    filename,
                    stop_reason,
                    timestamp,
                ),
            )
            if recording_session_id:
                db.execute(
                    """INSERT INTO recording_sessions
                       (id, order_code, evidence_type, started_at_utc, stopped_at_utc,
                        stop_reason, state, evidence_id)
                       VALUES (?, ?, ?, ?, ?, ?, 'SAVED', ?)
                       ON CONFLICT(id) DO UPDATE SET
                           stopped_at_utc = excluded.stopped_at_utc,
                           stop_reason = excluded.stop_reason,
                           state = 'SAVED',
                           evidence_id = excluded.evidence_id""",
                    (
                        recording_session_id,
                        order_code,
                        evidence_type,
                        timestamp,
                        timestamp,
                        stop_reason,
                        evidence_id,
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
    with get_db() as db:
        db.execute(
            """INSERT OR IGNORE INTO recording_sessions
               (id, order_code, evidence_type, started_at_utc, state)
               VALUES (?, ?, ?, ?, 'RECORDING')""",
            (session_id, order_code, evidence_type, utc_now()),
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
        item = db.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    if item is None:
        abort(404)
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
