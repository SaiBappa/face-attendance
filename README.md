# Aura — Face Attendance & AI host for airports

A wall-mounted iPad that recognises staff in ~0.5 s, clocks them IN / BREAK / BACK / OUT,
greets them personally, chats with staff and travellers in their own language, shows
location-specific content, and feeds a staff-behaviour and mood **Insights** dashboard.
Everything except the optional language services runs on one Mac (or any Docker host) on your LAN.

```
 iPad kiosk (Safari) ──HTTPS:8443──▶ caddy ──▶ attendance  (Starlette + SQLite: kiosk, admin, insights, brain)
                                                   │           │                    │
                                                   ▼           ▼                    ▼
                                             recognizer     listener          Jev (TypeSafe) + optional Claude
                                        (face match, FER+   (Whisper speech    (intent, sentiment, language,
                                         emotion, outfit)    -> text + lang)    wellbeing; free-form replies)
```

| Part | What it is |
|---|---|
| `recognizer/` | YuNet detection + SFace 128-d embeddings + **FER+ emotion** + dominant **outfit colour**, all OpenCV/ONNX, ARM-native. Stores one vector + a small face crop per enrolled photo, and learns each person's 2 best kiosk photos a day (kept 7 days) to follow changes in appearance. |
| `listener/` | **faster-whisper** (base, int8) — turns a voice clip into text and detects the language (99 languages). Model is baked into the image, works offline. |
| `attendance/` | `app.py` routes · `brain.py` conversation + greetings · `phrases.py` multilingual templates · `insights.py` analytics · `store.py` schema · `demo.py` demo data · `static/` kiosk, admin, insights pages |
| `caddy` | HTTPS with a local CA. iPad Safari only allows camera + microphone on HTTPS pages. |

## What Aura does

**On the wall (ambient mode)** — location name, big clock, a living "orb", a slideshow of the
images/videos you upload for that location, rotating info cards (prayer room, taxis, Wi-Fi…),
an announcements ticker and a live pulse (staff on duty, today's mood, chats today).

**When staff walk up** — recognised in under a second; the screen greets them by name with 2–3
lines chosen for the moment: birthday / work anniversary, a follow-up on something they told Aura
("you said you were tired yesterday — hope you rested"), a compliment on their outfit colour or
uniform, an upbeat response to their expression, and their punctuality (minutes early, on-time
streak, stayed late yesterday). Jev picks which line fits best. One tap to clock IN/BREAK/BACK/OUT,
then a personalised confirmation. Everything is spoken aloud (toggle per kiosk).

**When travellers walk up** — a rotating multilingual welcome, quick-question buttons generated
from the location's info cards, and "Talk to Aura": speak or type in any language, Aura answers
in that language (text + voice). Visitors stay anonymous: no identity, no face data, only an
aggregate mood reading per approach.

**Conversation brain** — Jev (TypeSafe's decision model) classifies each message: intent (16
kinds), sentiment, tiredness / illness / stress / celebration, the language (for Latin-script
text), and which info card answers the question. Replies come from Claude when
`ANTHROPIC_API_KEY` is set (free-form, uses the person's history and memories), otherwise from
built-in templates in 11 languages. Every exchange is stored per person (or per anonymous
visitor encounter), and wellbeing signals become short-lived *memories* Aura follows up on.

**Live flights** — each kiosk can show a departures/arrivals board (per location: both, departures,
arrivals or off), and Aura answers "is EK653 on time?", "my flight to Istanbul?" or "when does the
Doha flight land?" in the passenger's language, with gate, check-in desks or baggage belt. Data comes
from AeroDataBox (key from api.market or RapidAPI), your own FIDS JSON feed, or a realistic demo
schedule when no key is set.

**Assistance requests** — "🙋 Get help" on the kiosk (wheelchair, medical, lost item, lost child,
porter, security, talk to staff), or Aura offers a "call a team member" button when a conversation
shows someone needs help. The traveller sees live status ("Fathimath is on the way"). Staff work
requests on the **Assist board** (`/assist`, its own `ASSIST_PIN`) with SLA timers and a chime; new
requests can also be pushed to Slack/Teams/a WhatsApp gateway via `ASSIST_WEBHOOK_URL`.

**Ramp safety pack** — departments with safety rules (Admin → Setup; demo: Ground Handling, Security,
Facilities) get a checklist when they clock IN: PPE items (the camera auto-detects a hi-vis vest)
and "how rested do you feel?". A transparent fatigue-risk score (rest since last shift, hours in
24 h / 7 days, consecutive days, night work, recently telling Aura they were tired, the self-rating)
is shown to the worker with its reasons. Missing PPE or HIGH fatigue is flagged to supervisors
(`SAFETY_WEBHOOK_URL`, defaults to the assist webhook) and in Insights. By default it never blocks
clocking in — it's a prompt and a record, not a gate. The score is a rule-of-thumb risk indicator,
not a medical or regulatory fatigue-risk-management system.

**Location safety** — any safety rule can be linked to a location (Admin → Locations → a kiosk →
**Safety rule to enter**; create rules such as "Hangar" in Admin → Safety). Everyone entering there
(on IN and BACK) must meet it, on top of their department rule. Turn on **Enforce** and entry is
refused until all required PPE is confirmed: the kiosk shows "Entry refused", the server rejects
`/api/event` without a passing check at that kiosk in the last 15 minutes, and supervisors get a
"refused entry" alert. HIGH fatigue is still flagged only, never a lockout.

**Roster + supervisor alerts** — import the roster (Admin → Roster: CSV upload with preview, or
`POST /api/roster` from your HR/rostering system; overnight and split shifts supported). Punctuality
is then measured against each person's *rostered* start, and every shift gets an adherence status:
worked, late, left early, overtime, missed clock-out or no-show. A background scan raises
supervisor alerts — no-show (15 min after start), late arrival, missed clock-out, long break,
understaffed department (rostered vs on duty), assistance request waiting too long, flagged safety
check, expired security pass — each once, routed to the department's webhook (Admin → Alerts) or `ALERT_WEBHOOK_URL`, and
acknowledged/resolved in the Alerts feed. Staff can ask Aura "when is my next shift?".

**Expired security pass** — when the kiosk recognises someone whose security pass expiry (Admin →
People) is before today, it shows a full-screen flashing red *Security pass expired* warning, sounds a
20-second siren and speaks the warning so others on the floor notice, and swallows every tap, so no
IN / BREAK / BACK / OUT, safety check or chat is possible. The server enforces the same rule (the
event and safety endpoints return 403) and logs each incident as a high-severity `pass_expired`
supervisor alert, pushed to the webhook immediately.

```bash
curl -X POST http://<server>:8181/api/roster -H "X-Admin-Pin: <pin>" -H "Content-Type: application/json" \
  -d '{"replace": true, "shifts": [{"person": "Aishath Shiuna", "day": "2026-10-01", "start": "07:00", "end": "16:00",
       "position": "Immigration desk 3", "location": "Arrivals-Hall", "department": "Immigration"}]}'
```

**Insights (`/insights`)** — on-time %, arrival times, hours, breaks, staff & visitor mood trends,
mood by hour/weekday/location/department, arrivals heatmap, conversation intents & languages,
per-person drill-down (daily timeline, mood calendar, outfits, memories, full chat history) and
"needs attention" alerts: persistent low mood, sudden mood drop, repeated lateness, long breaks,
and kudos for on-time streaks.

## Configuration (`.env`, see `.env.example`)

| Variable | Default | Notes |
|---|---|---|
| `TYPESAFE_API_KEY` | — | Jev key. Without it Aura falls back to keyword rules. |
| `ANTHROPIC_API_KEY` | — | Optional. Enables free-form Claude replies in any language (`LLM_MODEL`, default `claude-opus-5`). |
| `AIRPORT_NAME`, `WIFI_INFO` | Velana… | Used in replies |
| `UNIFORM_COLORS` | — | e.g. `navy,white` → "Uniform on point today ✔" |
| `SHIFT_START`, `LATE_GRACE_MINUTES` | `08:00`, `5` | Default shift; each person can have their own in Admin → People |
| `WEEKEND_DAYS` | `fri,sat` | For day-of-week greetings |
| `MOOD_TRACKING` | `on` | `on` · `staff_only` · `visitors_only` · `off` |
| `INTERACTION_RETENTION_DAYS` | `365` | Conversations and mood readings older than this are purged on start |
| `FLIGHT_PROVIDER` | auto | `aerodatabox` · `json` · `demo` · `off`; empty = AeroDataBox if a key is set, else `json` if a URL is set, else demo |
| `FLIGHT_AIRPORT` | `MLE` | IATA code of the airport |
| `AERODATABOX_KEY` / `AERODATABOX_RAPIDAPI_KEY` | — | AeroDataBox FIDS via api.market or RapidAPI (paid tiers; check coverage for your airport) |
| `FLIGHT_FEED_URL` | — | Your own feed returning `{"flights": [...]}` in the shape `/api/flights` returns |
| `ASSIST_PIN` | = `ADMIN_PIN` | PIN for the `/assist` board, so desk agents don't need the admin PIN |
| `ASSIST_WEBHOOK_URL` | — | POSTs `{"text", "request"}` for each new assistance request |
| `ASSIST_SLA_MINUTES` | `5` | Response-time target shown on the board and in Insights |
| `SAFETY_WEBHOOK_URL` | = assist webhook | POSTs flagged safety checks (missing PPE / high fatigue) |
| `HIVIS_MIN_SHARE` | `0.12` | Share of fluorescent torso pixels that counts as "hi-vis seen" |
| `PASS_ALERT_MINUTES` | `10` | Repeat sightings of an expired-pass holder at the same kiosk within this window are one incident |
| `ALERT_WEBHOOK_URL` | — | Default destination for supervisor alerts (per-department routes override it) |
| `NO_SHOW_MINUTES` / `LATE_ALERT_MINUTES` | `15` / `10` | When a missing / late arrival becomes an alert |
| `CLOCKOUT_GRACE_MINUTES` / `BREAK_MAX_MINUTES` | `60` / `60` | Missed clock-out and long-break thresholds |
| `UNDERSTAFF_MIN` | `2` | Alert when a department has this many fewer on duty than rostered |
| `EARLY_LEAVE_MINUTES` | `15` | Leaving at least this long before the rostered end counts as an early leave |

Per-location content (headline, theme, info cards, images/videos, announcements, voice on/off)
is managed in **Admin → Locations**. Load **demo data** from Admin → Setup to show the system to
another airport before enrolling anyone; "Remove demo data" deletes only demo rows.

## Privacy & compliance — read before deploying

* Camera frames and voice clips are processed in memory and discarded. Stored: face *vectors*
  and a small **face crop** (face only, no background) per enrolled photo; for recognised staff,
  the **2 best kiosk photos per day, deleted after 7 days** (`DAILY_BEST`, `DAILY_KEEP_DAYS`), used
  to keep recognising people whose look changes; names, timestamps, mood *labels*, outfit colour
  names and conversation text. Visitor faces are never kept. Set `ADAPTIVE_LEARNING=off` to stop
  collecting daily photos; admins can review and remove any photo under **Admin → Enrol**.
* Only text and derived signals go to Jev / Claude — never images or audio.
* Each staff member can opt out of mood tracking (Admin → People); opting out also erases past
  mood readings. "Forget conversations" wipes their chat history and memories. Deleting a person
  removes face data, profile, moods and conversations (attendance records stay for payroll).
* **Emotion recognition of employees is restricted or banned in some jurisdictions** — e.g. the
  EU AI Act (Art. 5(1)(f)) prohibits it in the workplace except for medical/safety reasons. For
  such sites set `MOOD_TRACKING=visitors_only` or `off`. Get legal sign-off and staff consent,
  and use mood insights for wellbeing support, never for performance evaluation.
* Facial-expression models read *expressions*, not feelings, and are less accurate across some
  faces and lighting; treat trends over days as signals, single readings as noise.

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
with/without glasses), or upload photos. Good frontal light, one face in frame. Each photo shows
up below the camera as a face-crop thumbnail (✕ removes a bad one). The same screen shows the
**Learned at the kiosk** photos: every day the recognizer keeps the person's 2 sharpest,
most front-facing photos from confident matches, for 7 days, and matches against them too — so
a new beard, glasses or haircut keeps being recognised. A frame is only learned when it matches
clearly (`LEARN_MIN_SIMILARITY`), beats every other person by `LEARN_MARGIN`, and still resembles
the person's *enrolled* photos (`LEARN_ANCHOR_SIMILARITY`), so the model can't drift to someone else.

## 4. Set up each iPad kiosk

1. Safari → `https://<mac-ip>:8443/?kiosk=Arrivals-Hall` (one `kiosk=` name per location; the admin
   **Setup** tab generates links). Tap "Tap to wake Aura" once, allow camera **and microphone**.
2. Share → **Add to Home Screen**, open from the icon. It runs full-screen as its own app and
   reopens on the same `kiosk=` name. `/admin`, `/insights` and `/assist` install the same way
   (Chrome/Edge/Android also show an **Install** button); each is a separate app. A service worker
   keeps the last copy of each screen, so reopening it during a brief server outage still loads the
   page (or a self-retrying "can't reach the server" screen) instead of an error page. The API and
   photos are never cached.
3. **Settings → Accessibility → Guided Access → on**; in the kiosk triple-click the top button →
   Start. Staff can't leave the page.
4. Auto-Lock **Never**, keep on power, mount at face height, light from the front.

## Admin page (`/admin`)

| Tab | What |
|---|---|
| Today | who is IN / on BREAK / OUT now (with mood), auto-refresh |
| Log & Export | date range + employee filter, **Download CSV**, delete mistakes |
| People | profiles (department, shift, birthday, language, mood consent), rename, delete, forget |
| Enrol | camera capture or photo upload |
| Locations | per-kiosk headline, theme, info cards, images/videos, announcements |
| Conversations | everything said to Aura, filterable, CSV |
| Setup | kiosk links, AI engine status, demo data, privacy settings |

**Insights** (`/insights`, same PIN) is the analytics dashboard described above.

## Tuning (`docker-compose.yml` → `attendance.environment`)

| Variable | Default | Notes |
|---|---|---|
| `SIMILARITY_THRESHOLD` | `0.35` | SFace cosine. Same person ≈ 0.4–0.8, strangers < 0.3. Lower to 0.30 if staff are often "not recognised"; raise to 0.45 if a wrong name ever appears. |
| `DUPLICATE_WINDOW_SECONDS` | `60` | Same action for same person within this window is ignored |
| `ADMIN_PIN` | `2468` | Change it |
| `TZ` | `Indian/Maldives` | Timestamps stored in this zone |
| `ADAPTIVE_LEARNING` | `on` | Let the recognizer keep daily best photos of recognised staff |
| `LIVENESS` | `on` | Anti-spoofing (see below). `monitor` = score and alert but never block; `off` |
| `LIVENESS_THRESHOLD` | `0.5` | Liveness score (0..1) a matched face needs. Raise to 0.7 if photos ever get through; lower to 0.35 if real staff see "Please step up yourself" |
| `LIVENESS_FRAMES` | `2` | Matched frames averaged per verdict (a first frame scoring ≥ 0.9 passes straight away) |

### Liveness

A photo, print or phone screen showing a staff member's face is refused. The recognizer runs
MiniFASNet (Silent-Face-Anti-Spoofing, two small models built into the image from the authors'
original weights) on the face in front of the kiosk, **only when it already matches someone**, so
visitors and empty scenes cost nothing; it adds ~2 ms to a matched frame. A spoof shows "Please step
up yourself — photos and screens can't be used to clock in", reveals no name, is never learned as a
daily-best photo, and repeated attempts raise a 📵 **Photo / screen at kiosk** supervisor alert.
Every clock-in also carries a signed token from that live match, so `/api/event` can't be called for
someone the camera never saw.

To tune on a new camera: open the kiosk with `?debug=1` — the overlay shows each frame's liveness
score — or run with `LIVENESS=monitor` for a day and check the alerts before switching to `on`.

Recognizer (`recognizer.environment`): `DAILY_BEST` (2 photos/person/day), `DAILY_KEEP_DAYS` (7),
`LEARN_MIN_SIMILARITY` (0.45), `LEARN_MARGIN` (0.08 over the next person), `LEARN_ANCHOR_SIMILARITY`
(0.28 against enrolled photos), `LEARN_COOLDOWN_SECONDS` (15).

After a change: `docker compose up -d attendance` (or double-click `start.command`).

## Backup / move

```bash
docker run --rm -v face-attendance_attendance-data:/d -v "$PWD":/b alpine tar czf /b/attendance-db.tgz -C /d .
docker run --rm -v face-attendance_recognizer-data:/d  -v "$PWD":/b alpine tar czf /b/faces.tgz -C /d .
# attendance-data also holds uploaded kiosk media (/data/media) and all conversations
```

## Known limits

* Liveness is passive (single camera, no depth sensor): it stops photos, prints and phone/tablet
  screens, not a realistic 3D mask. Keep supervisors around high-security doors.
* Kiosk endpoints other than `/api/event` (e.g. `/api/talk`) are unauthenticated on the LAN, as before;
  put the kiosks on their own VLAN before connecting other networks.
* Info cards are answered in the admin's language; with Claude enabled they are translated on the fly.
* Dhivehi replies in `phrases.py` should be reviewed by a native speaker; iPads have no Dhivehi voice,
  so Dhivehi replies are shown but not spoken.
* Single shared admin PIN over LAN HTTPS; add users before exposing beyond the LAN.
* One Mac comfortably handles 5–10 kiosks at 1 frame/s each.
