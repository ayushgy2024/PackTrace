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

## Google sign-in and Drive setup

PackTrace requires Google sign-in by default. Authentication protects access to
the workspace, and each authenticated user has a separate evidence library and
Google Drive connection.

1. In Google Cloud Console, enable the **Google Drive API**, configure the OAuth consent screen, and create an
   **OAuth 2.0 Client ID** with application type **Web application**.
2. Add these exact authorized redirect URIs for the environments you use:
   - `http://localhost:5000/auth/google/callback`
   - `http://127.0.0.1:5000/auth/google/callback`
   - `https://pack-trace-gamma.vercel.app/auth/google/callback`
   - `http://localhost:5000/auth/google/drive/callback`
   - `http://127.0.0.1:5000/auth/google/drive/callback`
   - `https://pack-trace-gamma.vercel.app/auth/google/drive/callback`
3. Add the non-sensitive `.../auth/drive.file` scope to the consent screen's
   Data Access section. This lets PackTrace manage only files it creates; it
   cannot browse unrelated Drive files.
4. Set the values in your PowerShell session before starting locally:

```powershell
$env:GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
$env:GOOGLE_CLIENT_SECRET="your-google-client-secret"
$env:PACKTRACE_SECRET_KEY="use-a-long-random-secret-here"
$env:PACKTRACE_TOKEN_ENCRYPTION_KEY="use-a-different-long-random-secret-here"
# Recommended for internal use; separate multiple addresses with commas.
$env:PACKTRACE_ALLOWED_EMAILS="your.name@gmail.com"
.\.venv\Scripts\python.exe app.py
```

For Vercel, add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`PACKTRACE_SECRET_KEY`, `PACKTRACE_TOKEN_ENCRYPTION_KEY`, and `DATABASE_URL` in
Project Settings → Environment Variables, then redeploy. `DATABASE_URL` must be
a persistent PostgreSQL connection string (for example, from Supabase or Neon).
`PACKTRACE_ALLOWED_EMAILS` is optional: when blank, any Google account with a
verified email can sign in. Never put real secrets in `.env.example`, GitHub, or
source code. Do not rotate `PACKTRACE_TOKEN_ENCRYPTION_KEY` without asking users
to reconnect Drive.

For localhost-only UI development without Google, set
`PACKTRACE_DEV_AUTH_BYPASS=1`. This bypass is restricted to `localhost` and
`127.0.0.1`; it does not bypass a Vercel deployment. To intentionally disable
authentication everywhere, set `PACKTRACE_AUTH_REQUIRED=0`.

## Storage

- Local development metadata: `instance/packtrace.sqlite3`
- Production metadata and encrypted Drive refresh tokens: PostgreSQL through
  `DATABASE_URL`
- Drive-connected recordings: the signed-in user's `PackTrace Evidence` folder
- Local fallback recordings: `instance/private_evidence/`
- Files use generated UUID names rather than order numbers.
- Recordings are SHA-256 hashed before upload and Drive files are size-verified
  before evidence is marked `VERIFIED`.
- The application contains no delete path for unverified evidence.

The `instance/` directory is intentionally ignored by Git.

### Direct Drive uploads on Vercel

Vercel functions have a read-only application directory. PackTrace therefore
uses `/tmp/packtrace` when it detects Vercel so the application can start and
the scanning interface can be tested. Files in `/tmp` are not
durable: the SQLite database and recordings can disappear whenever Vercel
recycles or moves a function instance. Do not rely on this temporary storage
for production evidence. Once Drive is connected, recordings use Google's
resumable upload protocol and travel directly from the browser to the user's
Drive, bypassing Vercel's request-body limit. Interrupted uploads are retained
in that signed-in browser session's IndexedDB queue and can be retried from
**Uploads**; the user's queued blobs are removed from the device at sign-out. PackTrace
stores only metadata in PostgreSQL and never marks evidence verified until it
confirms the Drive file, its folder, and its byte size.
