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
the authentication/scanning interface can be tested. Files in `/tmp` are not
durable: the SQLite database and recordings can disappear whenever Vercel
recycles or moves a function instance. Do not rely on this temporary storage
for production evidence. A production Vercel deployment must use a persistent
cloud database and object storage, with recordings uploaded directly from the
browser to avoid serverless request-size limits.

## Google authentication

PackTrace uses Google OpenID Connect and requests only `openid`, `profile`, and
`email`. Create an OAuth client in Google Cloud Console:

1. Create or select a Google Cloud project.
2. Configure the OAuth consent/branding screen and add your Google account as a
   test user while the app is in testing mode.
3. Create an OAuth client with application type **Web application**.
4. Add this local authorized redirect URI exactly:

   `http://localhost:5000/auth/google/callback`

5. For Vercel, also add:

   `https://pack-trace-gamma.vercel.app/auth/google/callback`

After adding or changing Vercel environment variables, redeploy the project so
the new deployment receives them.

Set credentials for the current PowerShell window without saving them in Git:

```powershell
$env:PACKTRACE_SECRET_KEY = "replace-with-a-long-random-secret"
$env:GOOGLE_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
$env:GOOGLE_CLIENT_SECRET = "your-client-secret"
$env:PACKTRACE_ALLOWED_EMAILS = "your-google-email@example.com"
.\.venv\Scripts\python.exe app.py
```

Open <http://localhost:5000>. The redirect URI must exactly match the hostname,
scheme, port, path, and trailing-slash form configured in Google Cloud.

For camera testing before Google credentials are ready, an explicit local-only
bypass is available. It is rejected on non-local hostnames and must never be
enabled in Vercel:

```powershell
$env:PACKTRACE_DEV_AUTH_BYPASS = "1"
.\.venv\Scripts\python.exe app.py
```

## Not yet connected

Google OAuth and Drive resumable uploads require a Google OAuth client ID, client secret, and authorized deployment URL. Until configured, evidence remains locally marked `PENDING_UPLOAD`; the interface never falsely presents it as remotely verified.
