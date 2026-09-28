#!/bin/bash
# Start (or restart) the Face Attendance stack. Double-click any time — e.g. after a reboot.
set -u
cd "$(dirname "$0")"
export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin:$HOME/Applications/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin"

echo "Starting Docker Desktop if needed…"
open -a Docker 2>/dev/null || true
for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
if ! docker info >/dev/null 2>&1; then echo "Docker isn't running. Open Docker Desktop and try again."; read -r -p "Press Enter to close…" _; exit 1; fi

echo "Building + starting containers (first build downloads the face models, ~1–3 min)…"
docker compose up -d --build --remove-orphans

echo -n "Waiting for the recognizer to be ready "
for i in $(seq 1 60); do
  curl -s -o /dev/null http://localhost:8181/api/health && curl -s http://localhost:8181/api/health | grep -q '"ok": *true' && break
  echo -n "."; sleep 2
done; echo

IP=""
for IF in $(networksetup -listallhardwareports 2>/dev/null | awk '/Device/{print $2}'); do
  IP=$(ipconfig getifaddr "$IF" 2>/dev/null) && [[ -n "$IP" ]] && break
done

PIN=$(awk -F= '/^ADMIN_PIN/{print $2}' .env 2>/dev/null); PIN=${PIN:-2468}
echo
echo "── Face Attendance is running ──────────────────────"
echo "Admin (this Mac):  http://localhost:8181/admin      PIN: $PIN"
[[ -n "$IP" ]] && echo "Admin (iPads):     https://$IP:8443/admin"
[[ -n "$IP" ]] && echo "Kiosk (iPads):     https://$IP:8443/?kiosk=Staff-Entrance"
echo "Insights:          http://localhost:8181/insights"
echo "Health check:      curl http://localhost:8181/api/health"
echo
echo "Enrol staff and see reports from the Admin page. First enrol needs a face in front of the camera."
open "http://localhost:8181/admin" >/dev/null 2>&1 || true
read -r -p "Press Enter to close…" _
