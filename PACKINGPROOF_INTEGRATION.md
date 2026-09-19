# PackingProof integration notes

PackTrace is an independent, English-only Python and browser implementation.
Its scan-to-record state machine is informed by the architecture of
[PackingProof Desktop](https://github.com/PackingProof/PackingProof-Desktop),
which is licensed under GNU AGPL-3.0.

Integrated concepts:

- First valid AWB scan starts a recording.
- The triggering code remains locked while it stays visible.
- After the label leaves the frame, scanning the same AWB stops and saves.
- A different barcode cannot stop the active recording.
- JD base-waybill and package-suffix forms can refer to the same recording.
- Keyboard-mode USB barcode scanners work alongside camera recognition.
- SQLite uses WAL mode, recording-session audit rows, indexed AWB lookup,
  stop reasons, SHA-256 hashes, and duplicate-AWB warnings.
- Local recordings are retained until a separately verified remote copy exists.

Not imported because it is specific to the Windows desktop architecture:

- WPF user interface and Windows global keyboard hooks
- DirectShow, Media Foundation, GPU encoders, FFmpeg and LibVLC packaging
- Windows installer, launcher and incremental updater
- LAN workstation/mobile pairing and NAS archive services
- Kuaidi Assistant userscript, speech engines and Chinese localization

No PackingProof name, icon, screenshot, or other official brand asset is used as
PackTrace product identity. This integration does not imply upstream endorsement.
