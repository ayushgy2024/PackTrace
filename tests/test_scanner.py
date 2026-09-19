import io
from pathlib import Path
import tempfile
import unittest
import uuid

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

    def post_evidence(self, order_code: str, session_id: str = "") -> dict:
        response = self.client.post(
            "/api/evidence",
            data={
                "order_code": order_code,
                "evidence_type": "OUTBOUND",
                "duration_seconds": "8",
                "recording_session_id": session_id,
                "stop_reason": "SAME_AWB_RESCAN",
                "video": (io.BytesIO(b"webm-test-recording"), "evidence.webm"),
            },
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

if __name__ == "__main__":
    unittest.main()
