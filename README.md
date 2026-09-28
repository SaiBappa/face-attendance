# Face Attendance — iPad kiosk + local face recognition

Simple IN / BREAK / BACK / OUT attendance for a shop floor. Staff look at an iPad, are
recognised within ~0.5 s, and tap one big button. Everything runs on one Mac (or any
Docker host) on your LAN. No cloud, no per-seat licence.

```
 iPad (Safari kiosk page) ──HTTPS:8443──▶ caddy ──▶ attendance (Python/Starlette + SQLite)
                                                          │ REST
                                                          ▼
                                                    recognizer (OpenCV YuNet + SFace, ARM-native)
```

| Part | What it is |
|---|---|
| `recognizer/` | Face detection (YuNet) + 128-d face embeddings (SFace) via OpenCV. Runs natively on Apple Silicon and x86. Stores one vector per enrolled photo under a Docker volume — no photos kept. |
| `attendance/` | ~330 lines of Python: kiosk page, admin page, attendance log, CSV export. |
| `caddy` | HTTPS with a local CA. iPad Safari only allows the camera on HTTPS pages. |
| `run.command` | One-click: installs Docker Desktop if missing, sets your LAN IP, builds, starts, opens admin. |
| `start.command` | Day-to-day start/restart (e.g. after a reboot). |

> Why not CompreFace? We tried it first. Its ML images are x86-only and need AVX, which
> Apple Silicon/Rosetta doesn't provide — the core never became healthy. The OpenCV
> recognizer replaces it with the same API surface, so nothing else changed.

## 1. Run it (macOS)

1. Put this folder somewhere permanent, e.g. `~/Documents/face-attendance`.
2. Double-click **`run.command`**. (If macOS refuses: right-click → Open. If it says "no
   access privileges": in Terminal run `chmod +x ~/Documents/face-attendance/*.command` once.)
3. First run: ~600 MB Docker Desktop download if you don't have it, then a 2–5 min image build.
   It ends with **"Face Attendance is running"** and opens `http://localhost:8181/admin` (PIN `2468`).

Any other Docker host: `docker compose up -d --build`, then edit the IP in `Caddyfile`.

### Ports
| Port | What |
|---|---|
| 8181 | Admin + kiosk over plain HTTP (use from the server itself) |
| 8443 | Same, over HTTPS — what the iPads use |

## 2. Trust the HTTPS certificate on each iPad (once per iPad)

```bash
cd ~/Documents/face-attendance
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```
AirDrop `caddy-root.crt` to the iPad → *Profile Downloaded* → **Settings → General → VPN &
Device Management → Install** → then **Settings → General → About → Certificate Trust
Settings** → enable full trust for *Caddy Local Authority*.

## 3. Enrol staff

Open `https://<mac-ip>:8443/admin` (or `http://localhost:8181/admin` on the Mac), PIN, **Enrol** tab:
type the name → **Start camera** → **Capture & add** 3–5 times (front, slight left/right,
with/without glasses), or upload photos. Good frontal light, one face in frame.

## 4. Set up each iPad kiosk

1. Safari → `https://<mac-ip>:8443/?kiosk=Floor-1` (one `kiosk=` name per location; the admin
   **Setup** tab generates links). Allow camera.
2. Share → **Add to Home Screen**, open from the icon.
3. **Settings → Accessibility → Guided Access → on**; in the kiosk triple-click the top button →
   Start. Staff can't leave the page.
4. Auto-Lock **Never**, keep on power, mount at face height, light from the front.

## Admin page (`/admin`)

| Tab | What |
|---|---|
| Today | who is IN / on BREAK / OUT now, auto-refresh |
| Log & Export | date range + employee filter, **Download CSV**, delete mistakes |
| Employees | list, photo counts, rename, delete |
| Enrol | camera capture or photo upload |
| Setup | kiosk links, recognizer health |

## Tuning (`docker-compose.yml` → `attendance.environment`)

| Variable | Default | Notes |
|---|---|---|
| `SIMILARITY_THRESHOLD` | `0.35` | SFace cosine. Same person ≈ 0.4–0.8, strangers < 0.3. Lower to 0.30 if staff are often "not recognised"; raise to 0.45 if a wrong name ever appears. |
| `DUPLICATE_WINDOW_SECONDS` | `60` | Same action for same person within this window is ignored |
| `ADMIN_PIN` | `2468` | Change it |
| `TZ` | `Indian/Maldives` | Timestamps stored in this zone |

After a change: `docker compose up -d attendance` (or double-click `start.command`).

## Backup / move

```bash
docker run --rm -v face-attendance_attendance-data:/d -v "$PWD":/b alpine tar czf /b/attendance-db.tgz -C /d .
docker run --rm -v face-attendance_recognizer-data:/d  -v "$PWD":/b alpine tar czf /b/faces.tgz -C /d .
```

## Privacy

Camera frames are matched and discarded. Only face vectors (not images) and names + timestamps are
stored. Inform staff; most jurisdictions treat face data as biometric.

## Known limits

* No liveness check — a printed photo could clock someone in. Fine with supervisors around.
* Single shared admin PIN over LAN HTTPS; add users before exposing beyond the LAN.
* One Mac comfortably handles 5–10 kiosks at 1 frame/s each.
