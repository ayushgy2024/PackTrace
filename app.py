from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, url_for
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix
import zxingcpp


BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's deployed application directory is read-only.  /tmp is writable, but
# it is intentionally ephemeral and is only a bridge until durable cloud
# database/object storage is configured.
DEFAULT_DATA_DIR = Path("/tmp/packtrace") if IS_VERCEL else BASE_DIR / "instance"
INSTANCE_DIR = Path(os.environ.get("PACKTRACE_DATA_DIR", DEFAULT_DATA_DIR))
VIDEO_DIR = INSTANCE_DIR / "private_evidence"
DATABASE = INSTANCE_DIR / "packtrace.sqlite3"

app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
Image.MAX_IMAGE_PIXELS = 20_000_000


@app.get("/health")
def health():
    return jsonify(status="ok")


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
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
        if "stop_reason" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''")
        if "source_type" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN source_type TEXT NOT NULL DEFAULT 'web'")
        for name, definition in (
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("location_accuracy_m", "REAL"),
            ("location_recorded_at_utc", "TEXT"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE evidence ADD COLUMN {name} {definition}")
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(recording_sessions)")}
        for name, definition in (
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
                "SELECT COUNT(*) FROM evidence WHERE order_code = ?", (order_code,)
            ).fetchone()[0]
            db.execute(
                """INSERT INTO evidence
                   (id, order_code, evidence_type, recorded_at_utc, duration_seconds,
                    size_bytes, sha256, mime_type, local_filename, state, stop_reason,
                    source_type, latitude, longitude, location_accuracy_m,
                    location_recorded_at_utc, created_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD', ?, 'web',
                           ?, ?, ?, ?, ?)""",
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
                       (id, order_code, evidence_type, started_at_utc, stopped_at_utc,
                        stop_reason, state, evidence_id, latitude, longitude,
                        location_accuracy_m, location_recorded_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, 'SAVED', ?, ?, ?, ?, ?)
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
        db.execute(
            """INSERT OR IGNORE INTO recording_sessions
               (id, order_code, evidence_type, started_at_utc, state, latitude,
                longitude, location_accuracy_m, location_recorded_at_utc)
               VALUES (?, ?, ?, ?, 'RECORDING', ?, ?, ?, ?)""",
            (
                session_id,
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
