# PackTrace Python

Flask and SQLite web application for recording outbound packing and RTO evidence.

The recording workflow opens the camera first, reads an AWB/tracking number from
QR codes or common shipping barcodes, confirms two matching detections, and then
starts recording automatically. It uses the browser's native `BarcodeDetector`
when available, a locally bundled ZXing-JS reader otherwise, and a local Python
ZXing-C++ endpoint as a third decoding layer. The Python fallback stays active
even when the browser decoder is unavailable. A label photo can also be selected
for difficult scans, and manual tracking-number entry remains available for
damaged or unreadable labels.

The first successful AWB scan starts recording automatically. Move the label
out of view; once the scanner is armed, showing the same AWB again stops the
recording, saves the video, and inserts its metadata into the SQLite evidence
database. A different barcode cannot stop the active recording.

## Run

```powershell
cd "D:\3uToolsV3\Project - RTO Videos\packtrace-python"
.\.venv\Scripts\python.exe app.py
```

Open <http://127.0.0.1:5000>.

Flask 3.1.3 is pinned in `requirements.txt`. If it is not already available:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Google sign-in setup

PackTrace requires Google sign-in by default. Authentication protects access to
the workspace, but authenticated users currently share the same evidence
library; per-user data separation is not implemented yet.

1. In Google Cloud Console, configure the OAuth consent screen and create an
   **OAuth 2.0 Client ID** with application type **Web application**.
2. Add these exact authorized redirect URIs for the environments you use:
   - `http://localhost:5000/auth/google/callback`
   - `http://127.0.0.1:5000/auth/google/callback`
   - `https://pack-trace-gamma.vercel.app/auth/google/callback`
3. Set the values in your PowerShell session before starting locally:

```powershell
$env:GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
$env:GOOGLE_CLIENT_SECRET="your-google-client-secret"
$env:PACKTRACE_SECRET_KEY="use-a-long-random-secret-here"
# Recommended for internal use; separate multiple addresses with commas.
$env:PACKTRACE_ALLOWED_EMAILS="your.name@gmail.com"
.\.venv\Scripts\python.exe app.py
```

For Vercel, add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and
`PACKTRACE_SECRET_KEY` in Project Settings → Environment Variables, then
redeploy. `PACKTRACE_ALLOWED_EMAILS` is optional: when blank, any Google account
with a verified email can sign in. Never put real secrets in `.env.example`,
GitHub, or source code.

For localhost-only UI development without Google, set
`PACKTRACE_DEV_AUTH_BYPASS=1`. This bypass is restricted to `localhost` and
`127.0.0.1`; it does not bypass a Vercel deployment. To intentionally disable
authentication everywhere, set `PACKTRACE_AUTH_REQUIRED=0`.

## Storage

- Metadata: `instance/packtrace.sqlite3`
- Private recordings: `instance/private_evidence/`
- Files use generated UUID names rather than order numbers.
- Recordings are SHA-256 hashed while being saved.
- The application contains no delete path for unverified evidence.

The `instance/` directory is intentionally ignored by Git.

### Vercel storage limitation

Vercel functions have a read-only application directory. PackTrace therefore
uses `/tmp/packtrace` when it detects Vercel so the application can start and
the scanning interface can be tested. Files in `/tmp` are not
durable: the SQLite database and recordings can disappear whenever Vercel
recycles or moves a function instance. Do not rely on this temporary storage
for production evidence. A production Vercel deployment must use a persistent
cloud database and object storage, with recordings uploaded directly from the
browser to avoid serverless request-size limits.

## Not yet connected

Persistent cloud storage is not connected yet. Evidence remains locally marked
`PENDING_UPLOAD`; the interface never falsely presents it as remotely verified.
