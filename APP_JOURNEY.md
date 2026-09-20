# PackTrace — Product Journey and Version Progress

This is the living development record for PackTrace. Update it whenever a
feature, fix, design change, test result, deployment change, or release decision
is made. A task is complete only when its acceptance checks pass.

## Current position

**Stable backup:** `packtrace-python v1.00.zip`  
**Production Git branch:** `main`  
**Internal version label:** Version 1.2  
**Branch policy:** `main` and `version-1.2` stay aligned to the same release commit  
**Current development phase:** Version 1.2 authentication and mobile QA

```text
PackTrace journey

Foundation       Scanner          Workflow         Deployment       Version 1.2
[██████████] → [██████████] → [██████████] → [██████████] → [░░░░░░░░░░]
   Complete         Complete*         Complete          Live             Planned

* Basic scanning exists, but real courier-label reliability needs improvement.
```

## Version history

### Foundation — Python web application

Status: **Complete**

- Replaced the earlier mobile-app direction with a Python Flask web application.
- Added an English-only interface for outbound and RTO evidence.
- Added local SQLite metadata storage and private video-file storage.
- Created dashboard, evidence library, upload queue, settings, and recording UI.
- Added a virtual environment and pinned Python dependencies.

### Scanner foundation

Status: **Complete, reliability improvements required**

- Added QR and common 1D/2D barcode support.
- Added browser `BarcodeDetector` support when the browser provides it.
- Bundled ZXing Browser as the browser fallback.
- Added Python ZXing-C++ image decoding as the server fallback.
- Added label-photo scanning and keyboard-mode USB scanner support.
- Added basic image contrast, sharpening, rotation, inversion, and scaling passes.
- Added tests for clean QR and Code 128 samples.
- Known limitation: real labels can be rotated, reflective, wrinkled, blurred,
  contain multiple codes, or contain bars that become too small after resizing.

### Scan-to-record workflow

Status: **Complete**

- First valid AWB scan starts recording automatically.
- The label must leave the camera view before recording can be stopped by scan.
- Scanning the same AWB again stops and automatically saves the recording.
- A different AWB cannot stop the active recording.
- Added recording-session audit data, stop reasons, hashes, and duplicate warnings.
- Added manual AWB entry for damaged or unreadable labels.

### Open-source workflow reference

Status: **Complete**

- Studied the PackingProof Desktop workflow.
- Reimplemented applicable workflow ideas in the English PackTrace web interface.
- Added attribution and kept PackTrace independent and unofficial.

### GitHub and deployment

Status: **Complete for testing**

- Created the Git repository and pushed it to `ayushgy2024/PackTrace`.
- Connected the project to Vercel.
- Updated Vercel runtime paths to use writable `/tmp` storage.
- Removed Google sign-in and made the application directly accessible.
- Created a complete local Version 1.00 ZIP backup, including Git metadata,
  virtual environment, database, recordings, and source files.
- Important limitation: Vercel `/tmp` storage is temporary. Production evidence
  still requires durable cloud database and object storage.

## Git milestones

| Commit | Change | Included in Version 1.00 |
|---|---|---|
| `5e2b0e6` | Initial PackTrace Python web application | Yes |
| `42c1d51` | Google authentication experiment | Superseded |
| `5b9d32b` | Vercel writable runtime storage | Yes |
| `385157f` | Google authentication removed | Yes |

## Version 1.00 — preserved baseline

Status: **Frozen backup available**

Version 1.00 is the preserved working baseline in the local ZIP backup. The
GitHub `main` branch now follows the current approved Version 1.2 source so that
Vercel production deployments are not left on an older branch revision.

Baseline capabilities:

- English Flask web interface.
- Camera recording from a mobile or desktop browser.
- QR, Data Matrix, and common linear-barcode decoding paths.
- Automatic recording start on the first accepted AWB.
- Automatic stop and save when the same AWB is scanned again.
- Local evidence database, file hashing, review, and duplicate-AWB warnings.
- Public Vercel test deployment without Google authentication.

## Version 1.2 — planned experience

Status: **Development snapshot approved for commit and push; not deployed**

```text
Version 1.2 progress

Planning                 [██████████] 100%
High-visibility UI       [█████████░]  90%
Barcode reliability      [███░░░░░░░]  30%
Date and time             [████████░░]  80%
Geotagging                [████████░░]  80%
Mobile flash control      [███████░░░]  70%
Voice status prompts      [████████░░]  80%
Mobile-browser QA         [███░░░░░░░]  30%
Source-control approval   [██████████] 100%

Overall                   [███████░░░]  75%
```

### 1. High-visibility PackTrace camera interface

The supplied orange screen was used only to understand the required feature
hierarchy. The PackTrace result uses an original structure and visual identity.

Planned elements:

- Full-screen live camera preview with a dark lower control area.
- Electric chartreuse scan gate and primary controls for strong visibility in
  bright warehouses, dim rooms, and visually busy camera scenes.
- Large elapsed recording timer.
- Visible FPS and camera-resolution indicators when available.
- Clear “waiting for AWB scan” and recording states.
- Large central record/package control.
- Flash control, camera-switch control, and manual-entry control.
- Mobile safe-area spacing for notches and browser controls.
- PackTrace branding; do not copy the reference app’s name or protected assets.

Acceptance checks:

- [ ] Fits common Android and iPhone portrait screens without horizontal scroll.
- [ ] Camera preview remains usable when browser toolbars expand or collapse.
- [x] Scanning, recording, stopping, saving, and manual entry remain accessible.
- [x] Controls have clear pressed, disabled, unavailable, and active states.
- [x] Desktop layout remains usable.

### 2. Real-label barcode reliability

Primary example: Flipkart label with AWB `FMPP3745118B20`, a narrow linear
barcode, and a separate square Data Matrix symbol.

Planned work:

- Process every detected code instead of only the first browser result.
- Prefer an AWB-shaped linear-barcode result when several symbols are present.
- Preserve more camera resolution for narrow 1D bars.
- Add barcode-focused regions of interest and multi-scale decoding.
- Avoid blocking browser scans while a server fallback request is in flight.
- Improve rotated-label and low-contrast preprocessing.
- Add real-label regression fixtures with private information redacted.
- Display actionable guidance for blur, glare, distance, and unsupported codes.

Acceptance checks:

- [ ] Reads the intended AWB rather than unrelated routing-code content.
- [ ] Reads landscape and portrait labels at 0°, 90°, 180°, and 270°.
- [ ] Handles a label containing both a Data Matrix and a linear barcode.
- [ ] Does not start recording from an unrelated product barcode.
- [ ] Same-AWB rescan reliably stops the correct recording.
- [ ] Synthetic tests and real-label tests pass.

### 3. Date and time

Planned work:

- Show the device’s live local date and time on the camera interface.
- Store recording start and stop timestamps in UTC.
- Show the local timezone alongside the human-readable display.
- Decide separately whether date/time must be permanently burned into the video.

Acceptance checks:

- [x] Display updates without affecting scanner performance.
- [x] Saved evidence contains reliable UTC timestamps.
- [ ] Timezone and local display are understandable and consistent.
- [ ] Permission denial is not relevant to date/time operation.

### 4. Geotagging

Privacy rule: request location only after a clear user action and explain why it
is needed. Never silently block recording when location is unavailable.

Planned work:

- Ask for browser geolocation permission.
- Capture latitude, longitude, accuracy, and capture time.
- Store coordinates with the recording-session and evidence records.
- Show permission, locating, captured, unavailable, and denied states.
- Provide a setting to disable location collection.
- Decide whether the visible overlay shows coordinates, an approximate place,
  or a privacy-safe location label.

Acceptance checks:

- [x] Recording still works if location permission is denied.
- [x] Location data is attached to the correct evidence record.
- [x] Accuracy and timestamp are stored with the coordinates.
- [x] No location data is collected before permission.
- [x] UI clearly shows when geotagging is active.

### 5. Mobile flash control

Browser limitation: torch support depends on the phone, selected rear camera,
browser, and HTTPS context. The control must gracefully show “unavailable.”

Planned work:

- Inspect the active video track’s `torch` capability.
- Apply the torch constraint when supported.
- Provide clear on, off, and unavailable states.
- Turn the torch off when the stream closes or the camera changes.
- Never treat lack of torch support as a recording failure.

Acceptance checks:

- [x] Flash control enables only when the active camera exposes torch support.
- [ ] Tapping toggles the physical torch on supported Android devices.
- [ ] Unsupported iPhone/browser combinations fail gracefully.
- [ ] Torch is released when leaving the camera screen.

### 6. Mobile QA and release

- [ ] Test Android Chrome over HTTPS.
- [ ] Test iPhone Safari over HTTPS.
- [ ] Test camera and microphone permission denial.
- [ ] Test geolocation permission allowed and denied.
- [ ] Test torch-supported and torch-unsupported devices.
- [ ] Test Flipkart-style multi-code labels.
- [ ] Test start scan, label removal, same-AWB stop scan, and automatic save.
- [ ] Verify no Version 1.00 regression.
- [x] Obtain explicit approval before committing.
- [x] Obtain explicit approval before pushing.
- [ ] Obtain explicit approval before deploying.

## Change log template

Copy this block for every Version 1.2 edit:

```text
Date:
Version:
Area:
Requested change:
Files changed:
What changed:
Reason:
Tests performed:
Result:
Known limitations:
Commit status: Uncommitted / Approved / Committed
Deployment status: Not deployed / Preview / Production
```

## Version 1.2 edit log

### 19 September 2026 — High-visibility camera UI and device metadata

- **Version:** 1.2 local working copy
- **Area:** Camera capture, evidence metadata, mobile controls
- **Requested change:** Create an original camera screen informed by the supplied
  feature reference, without copying its exact layout, and retain geotagging,
  date/time, and flash control.
- **Files changed:** `templates/base.html`, `templates/_evidence_row.html`,
  `static/css/app.css`, `static/js/app.js`, `app.py`, `tests/test_scanner.py`, and
  this journey document.
- **What changed:** Added an original PackTrace Proof Capture composition with a
  floating telemetry bar, segmented scan gate, evidence timestamp card,
  asymmetric control dock, live local date/time, camera FPS/resolution display,
  browser geolocation states, stored coordinates/accuracy/time, and
  capability-aware torch control. Electric chartreuse replaces orange as the
  persistent high-visibility signal; red is reserved for active recording.
- **Privacy behavior:** Location is requested only after the user opens the camera;
  denial or unavailability does not block scanning or recording.
- **Data behavior:** Latitude, longitude, accuracy, and location timestamp are
  stored in both recording-session and saved-evidence records when available.
- **Tests performed:** Nine Python tests passed, Python compilation passed,
  JavaScript syntax passed, migration paths passed, and desktop browser visual QA
  passed with a live camera feed. Android/iPhone hardware QA remains outstanding.
- **Known limitation:** The visible date/location HUD is not yet burned permanently
  into the recorded video pixels; it is displayed live and stored as metadata.
- **Commit status:** Explicitly approved for commit and push on 19 September 2026
- **Deployment status:** Not deployed

### 19 September 2026 — Source-control checkpoint approved

- **Version:** 1.2 development snapshot
- **Area:** GitHub source control
- **Requested change:** Commit the completed local work, update this journey,
  and push the development branch to GitHub.
- **Scope:** High-visibility Proof Capture UI, geotag metadata, live date/time,
  capability-aware mobile torch control, database migrations, evidence display,
  tests, and project documentation.
- **Validation before commit:** Nine Python tests passed, Python compilation
  passed, JavaScript syntax passed, and `git diff --check` passed.
- **Commit status:** Approved; included in this checkpoint
- **Push status:** Approved for `origin/version-1.2`
- **Deployment status:** Not deployed; no approval to merge or deploy

### 20 September 2026 — Git and Vercel branch alignment

- **Version:** 1.2
- **Area:** GitHub and Vercel production source
- **Issue found:** The Version 1.2 commit was pushed only to `version-1.2`, while
  GitHub's default branch and Vercel production source remained `main` at the
  older Version 1.00 commit. The live site therefore returned the old interface.
- **Resolution:** Keep Version 1.2 as the internal product label, fast-forward
  `main` to the same release commit, and keep `main` and `version-1.2` aligned.
- **Verification before fix:** The live Vercel URL returned HTTP 200 but did not
  contain the Version 1.2 `PROOF CAPTURE` interface.
- **Commit status:** Approved
- **Push status:** Approved for both aligned branches
- **Deployment status:** Verified live on Vercel after the `main` push. The
  production URL returned HTTP 200 and contained the Version 1.2
  `PROOF CAPTURE` interface on the second deployment check.

### 20 September 2026 — Reliable same-AWB stop detection

- **Version:** 1.2 local working copy
- **Area:** Live barcode scanner and automatic recording stop
- **Reported issue:** The first AWB scan started recording, but presenting the
  same barcode again did not reliably stop the recording.
- **Root causes:** The browser path inspected only the first returned barcode on
  multi-code labels, and stop detection required eight consecutive absent scan
  cycles. Server-assisted cycles made the re-arm delay inconsistent.
- **Resolution:** Inspect and prioritize all browser/server results, prefer the
  currently recording AWB on multi-code labels, and use a short time-based label
  absence plus two blank scan cycles to arm the second scan.
- **Safety behavior:** A continuously visible AWB cannot immediately stop its own
  recording. The label must leave the view before the same AWB can stop and save.
- **Commit status:** Explicitly approved and included in the local commit
- **Deployment status:** Not deployed

### 20 September 2026 — Recording voice announcements

- **Version:** 1.2 local working copy
- **Area:** Operator feedback during capture
- **Requested change:** Speak “Recording Started” when capture begins and
  “Recording End” when capture stops.
- **Resolution:** Added English on-device speech announcements using the
  browser's built-in speech synthesis. Manual stops, same-AWB automatic stops,
  close actions, and unexpected recorder stops share a guarded end announcement
  so the phrase is not repeated. Voice selection prioritizes installed Indian
  English female voices, then recognized English female voices across Android,
  iPhone, Windows, and macOS. A clear higher-pitch fallback is used when the
  browser does not expose a named female voice.
- **Privacy behavior:** No text or audio is sent to the PackTrace server for
  speech generation. Voice availability and pronunciation depend on the device.
- **Known limitation:** The phone speaker announcement may be audible in the
  recorded video's microphone track, which can be useful as an evidence cue.
- **Commit status:** Explicitly approved and included in the local commit
- **Deployment status:** Not deployed

### 20 September 2026 — Recording-time scanner performance

- **Version:** 1.2 local working copy
- **Area:** Same-AWB scanning while video encoding is active
- **Reported issue:** Initial AWB scanning was responsive, but the same-AWB scan
  used to stop an active recording lagged or failed to register promptly.
- **Root cause:** During recording, the browser was encoding high-resolution VP9,
  decoding frames locally, creating JPEG frames, and awaiting the Python fallback
  response before scheduling the next local scan. Mobile devices therefore had a
  much heavier and partly blocking scan loop than they had before recording.
- **Resolution:** Server fallback scans now run asynchronously during recording,
  local detection continues without waiting for the network, the active AWB is
  protected against stale fallback responses, and recording prefers efficient
  VP8/native browser encoding at a controlled bitrate.
- **Reliability behavior:** Server fallback remains active more frequently while
  recording, but only a response belonging to the current active AWB session may
  affect that recording.
- **Commit status:** Explicitly approved and included in the local commit
- **Deployment status:** Not deployed

### 20 September 2026 — Save error hardening

- **Version:** 1.2 local working copy
- **Area:** Recorder stop transition and evidence upload
- **Reported issue:** Saving displayed “The string did not match the expected
  pattern” instead of completing or explaining the actual failure.
- **Likely triggers found:** Some mobile voices expose a language identifier that
  Safari rejects when the end announcement runs, and Vercel returns a non-JSON
  error page when a recording upload exceeds its 4.5 MB function payload limit.
- **Resolution:** Speech exceptions are now fully isolated from recording state,
  voice language tags are validated, server responses are parsed defensively,
  HTTP 413 receives an actionable message, and capture now targets efficient
  720p video at a controlled video/audio bitrate.
- **Remaining production limitation:** Arbitrarily long videos still require a
  direct browser-to-object-storage upload; Vercel Function uploads cannot exceed
  4.5 MB and temporary `/tmp` storage is not durable.
- **Commit status:** Explicitly approved and included in the local commit
- **Deployment status:** Not deployed

### 21 September 2026 — Google account sign-in

- **Version:** 1.2 local working copy
- **Area:** Workspace access and user identity
- **Requested change:** Allow users to sign in with their Google ID.
- **Resolution:** Added Google OpenID Connect login, a protected-by-default
  workspace, verified-email checks, 12-hour signed sessions, sign-out controls,
  and an optional email allowlist for internal use. The health endpoint remains
  public for deployment monitoring, while unauthenticated API requests return
  HTTP 401.
- **Configuration safety:** OAuth credentials and the Flask signing secret are
  supplied only through environment variables. A production deployment with
  missing credentials fails closed on the setup screen instead of exposing the
  evidence workspace.
- **Scope note:** Authentication identifies and authorizes entry to the shared
  PackTrace workspace. It does not yet partition evidence by user account.
- **Commit status:** Explicitly approved for commit and push
- **Deployment status:** Pending production deployment verification

## Working rules

1. Version 1.00 remains preserved in the local ZIP backup.
2. Version 1.2 is the internal product version; GitHub branches should not carry
   different application versions.
3. `main` is the production source branch and must stay aligned with the approved
   Version 1.2 release commit.
4. Every material edit is recorded in this document.
5. Local edits and tests do not imply permission to commit.
6. Commit, push, merge, and production deployment require explicit approval.
