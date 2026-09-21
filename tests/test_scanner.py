import io
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from PIL import Image
import zxingcpp

import app as app_module


class ScannerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_paths = (app_module.INSTANCE_DIR, app_module.VIDEO_DIR, app_module.DATABASE)
        app_module.INSTANCE_DIR = Path(self.temp_dir.name)
        app_module.VIDEO_DIR = app_module.INSTANCE_DIR / "private_evidence"
        app_module.DATABASE = app_module.INSTANCE_DIR / "packtrace.sqlite3"
        app_module.init_db()
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.app.config["TESTING"] = False
        app_module.INSTANCE_DIR, app_module.VIDEO_DIR, app_module.DATABASE = self.original_paths
        self.temp_dir.cleanup()

    def post_barcode(self, text: str, barcode_format) -> dict:
        barcode = zxingcpp.create_barcode(text, barcode_format)
        image = Image.fromarray(barcode.to_image(scale=5))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        payload.seek(0)
        response = self.client.post(
            "/api/scan",
            data={"frame": (payload, "camera-frame.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_reads_qr_tracking_number(self):
        result = self.post_barcode("AWB-QR-78421190", zxingcpp.BarcodeFormat.QRCode)
        self.assertEqual(result["codes"][0]["text"], "AWB-QR-78421190")

    def test_reads_code128_tracking_number(self):
        result = self.post_barcode("784211901234", zxingcpp.BarcodeFormat.Code128)
        self.assertEqual(result["codes"][0]["text"], "784211901234")

    def test_reads_code128_from_large_phone_photo(self):
        barcode = Image.fromarray(
            zxingcpp.create_barcode("AWB9876543210", zxingcpp.BarcodeFormat.Code128).to_image(scale=3)
        ).convert("RGB")
        photo = Image.new("RGB", (2400, 1800), "#e8e5df")
        photo.paste(barcode, ((photo.width - barcode.width) // 2, 600))
        payload = io.BytesIO()
        photo.save(payload, format="JPEG", quality=72)
        payload.seek(0)
        response = self.client.post(
            "/api/scan",
            data={"frame": (payload, "phone-photo.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["codes"][0]["text"], "AWB9876543210")

    def test_rejects_invalid_image(self):
        response = self.client.post(
            "/api/scan",
            data={"frame": (io.BytesIO(b"not-an-image"), "bad.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def post_evidence(self, order_code: str, session_id: str = "", **metadata) -> dict:
        data = {
            "order_code": order_code,
            "evidence_type": "OUTBOUND",
            "duration_seconds": "8",
            "recording_session_id": session_id,
            "stop_reason": "SAME_AWB_RESCAN",
            "video": (io.BytesIO(b"webm-test-recording"), "evidence.webm"),
        }
        data.update(metadata)
        response = self.client.post(
            "/api/evidence",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def test_recording_session_is_linked_to_saved_evidence(self):
        session_id = str(uuid.uuid4())
        start = self.client.post(
            "/api/recording-sessions",
            json={"id": session_id, "order_code": "AWB123456789", "evidence_type": "OUTBOUND"},
        )
        self.assertEqual(start.status_code, 201)
        saved = self.post_evidence("AWB123456789", session_id)
        with app_module.get_db() as db:
            session = db.execute(
                "SELECT state, stop_reason, evidence_id FROM recording_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            evidence = db.execute(
                "SELECT order_code, stop_reason, sha256 FROM evidence WHERE id = ?",
                (saved["id"],),
            ).fetchone()
        self.assertEqual(tuple(session), ("SAVED", "SAME_AWB_RESCAN", saved["id"]))
        self.assertEqual(evidence["order_code"], "AWB123456789")
        self.assertEqual(evidence["stop_reason"], "SAME_AWB_RESCAN")
        self.assertEqual(len(evidence["sha256"]), 64)

    def test_geotag_is_stored_with_evidence(self):
        saved = self.post_evidence(
            "GEO123456",
            latitude="28.6139",
            longitude="77.2090",
            location_accuracy_m="12.5",
            location_recorded_at_utc="2026-09-19T16:45:00.000Z",
        )
        with app_module.get_db() as db:
            evidence = db.execute(
                "SELECT latitude, longitude, location_accuracy_m, location_recorded_at_utc FROM evidence WHERE id = ?",
                (saved["id"],),
            ).fetchone()
        self.assertAlmostEqual(evidence["latitude"], 28.6139)
        self.assertAlmostEqual(evidence["longitude"], 77.2090)
        self.assertEqual(evidence["location_accuracy_m"], 12.5)
        self.assertEqual(evidence["location_recorded_at_utc"], "2026-09-19T16:45:00.000Z")

    def test_invalid_geotag_is_rejected(self):
        response = self.client.post(
            "/api/recording-sessions",
            json={
                "id": str(uuid.uuid4()),
                "order_code": "GEO123456",
                "evidence_type": "OUTBOUND",
                "latitude": 123,
                "longitude": 77.2,
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_awb_is_reported(self):
        first = self.post_evidence("DUPLICATE123")
        second = self.post_evidence("DUPLICATE123")
        self.assertEqual(first["duplicate_count"], 1)
        self.assertEqual(second["duplicate_count"], 2)
        page = self.client.get(second["redirect"])
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Duplicate warning", page.data)
        self.assertIn(b"Duplicate \xc3\x972", page.data)

    def test_all_english_pages_render(self):
        for path in ("/", "/evidence", "/uploads", "/settings"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        settings = self.client.get("/settings")
        self.assertIn(b"Open-source foundation", settings.data)
        self.assertIn(b"independent, unofficial web application", settings.data)

    def test_google_login_protects_pages_and_apis(self):
        app_module.app.config["TESTING"] = False
        app_module.app.config["AUTH_REQUIRED"] = True
        try:
            page = self.client.get("/")
            self.assertEqual(page.status_code, 302)
            self.assertTrue(page.headers["Location"].endswith("/login"))
            api = self.client.post("/api/scan")
            self.assertEqual(api.status_code, 401)
            self.assertEqual(api.get_json()["error"], "Google authentication is required.")
            health = self.client.get("/health")
            self.assertEqual(health.status_code, 200)
        finally:
            app_module.app.config["TESTING"] = True

    def test_authenticated_google_user_can_open_workspace(self):
        app_module.app.config["TESTING"] = False
        app_module.app.config["AUTH_REQUIRED"] = True
        try:
            with self.client.session_transaction() as browser_session:
                browser_session["user"] = {
                    "sub": "google-test-user",
                    "email": "operator@example.com",
                    "name": "Test Operator",
                    "picture": "",
                }
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Hello, Test", response.data)
            self.assertIn(b"operator@example.com", response.data)
        finally:
            app_module.app.config["TESTING"] = True

    def test_unconfigured_google_login_has_safe_setup_message(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Google sign-in unavailable", response.data)
        self.assertIn(b"Administrator setup required", response.data)

    def set_google_user(self, subject: str, email: str):
        with self.client.session_transaction() as browser_session:
            browser_session["user"] = {
                "sub": subject,
                "email": email,
                "name": subject,
                "picture": "",
            }
        app_module.upsert_user({"sub": subject, "email": email, "name": subject, "picture": ""})

    def test_evidence_library_is_partitioned_by_google_user(self):
        self.set_google_user("user-a", "a@example.com")
        self.post_evidence("PRIVATE-AWB-A")
        self.set_google_user("user-b", "b@example.com")
        self.post_evidence("PRIVATE-AWB-B")
        page = self.client.get("/evidence")
        self.assertIn(b"PRIVATE-AWB-B", page.data)
        self.assertNotIn(b"PRIVATE-AWB-A", page.data)

    def test_drive_upload_is_verified_before_evidence_insert(self):
        self.set_google_user("drive-user", "drive@example.com")
        with app_module.get_db() as db:
            db.execute(
                """INSERT INTO drive_connections
                   (owner_sub, email, encrypted_refresh_token, folder_id, connected_at_utc, updated_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("drive-user", "drive@example.com", "encrypted", "folder-123", app_module.utc_now(), app_module.utc_now()),
            )

        upload_id = str(uuid.uuid4())
        initiate_response = Mock(ok=True, status_code=200, headers={"Location": "https://upload.example/session"})
        with patch.object(app_module, "refresh_drive_access_token", return_value="access-token"), \
             patch.object(app_module, "ensure_drive_folder", return_value="folder-123"), \
             patch.object(app_module.http_requests, "post", return_value=initiate_response):
            initiated = self.client.post(
                "/api/drive/uploads/initiate",
                json={
                    "upload_id": upload_id,
                    "order_code": "DRIVE-AWB-123",
                    "evidence_type": "OUTBOUND",
                    "duration_seconds": 12,
                    "size_bytes": 1024,
                    "sha256": "a" * 64,
                    "mime_type": "video/webm",
                    "stop_reason": "SAME_AWB_RESCAN",
                },
            )
        self.assertEqual(initiated.status_code, 200)
        self.assertEqual(initiated.get_json()["upload_url"], "https://upload.example/session")

        drive_file_response = Mock(ok=True, status_code=200)
        drive_file_response.json.return_value = {
            "id": "drive-file-12345",
            "size": "1024",
            "parents": ["folder-123"],
            "webViewLink": "https://drive.google.com/file/d/drive-file-12345/view",
            "md5Checksum": "abc123",
        }
        with patch.object(app_module, "refresh_drive_access_token", return_value="access-token"), \
             patch.object(app_module.http_requests, "get", return_value=drive_file_response):
            completed = self.client.post(
                "/api/drive/uploads/complete",
                json={"upload_id": upload_id, "drive_file_id": "drive-file-12345"},
            )
        self.assertEqual(completed.status_code, 200)
        with app_module.get_db() as db:
            evidence = db.execute("SELECT * FROM evidence WHERE id = ?", (upload_id,)).fetchone()
        self.assertEqual(evidence["owner_sub"], "drive-user")
        self.assertEqual(evidence["state"], "VERIFIED")
        self.assertEqual(evidence["drive_file_id"], "drive-file-12345")

    def test_drive_refresh_tokens_are_encrypted_at_rest(self):
        with patch.dict(os.environ, {"PACKTRACE_TOKEN_ENCRYPTION_KEY": "test-only-encryption-secret"}):
            encrypted = app_module.encrypt_refresh_token("refresh-token-value")
            self.assertNotIn("refresh-token-value", encrypted)
            self.assertEqual(app_module.decrypt_refresh_token(encrypted), "refresh-token-value")

    def test_state_changing_api_requires_csrf_outside_test_mode(self):
        original_auth_required = app_module.app.config["AUTH_REQUIRED"]
        app_module.app.config["TESTING"] = False
        app_module.app.config["AUTH_REQUIRED"] = False
        try:
            self.client.get("/")
            payload = {
                "id": str(uuid.uuid4()),
                "order_code": "CSRF123456",
                "evidence_type": "OUTBOUND",
            }
            rejected = self.client.post("/api/recording-sessions", json=payload)
            self.assertEqual(rejected.status_code, 400)
            self.assertIn("security token", rejected.get_json()["error"])
            with self.client.session_transaction() as browser_session:
                token = browser_session["_csrf_token"]
            accepted = self.client.post(
                "/api/recording-sessions",
                json=payload,
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(accepted.status_code, 201)
        finally:
            app_module.app.config["AUTH_REQUIRED"] = original_auth_required
            app_module.app.config["TESTING"] = True

if __name__ == "__main__":
    unittest.main()
