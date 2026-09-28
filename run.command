#!/bin/bash
# =====================================================================================
#  Face Attendance — one-click install & run for macOS (double-click this file)
#    1. Installs Docker Desktop if missing (official download, no admin password)
#    2. Writes this Mac's LAN IP into the Caddyfile (HTTPS for the iPads)
#    3. Builds + starts the recognizer (OpenCV, ARM-native) + attendance + Caddy
#    4. Waits for health and opens the admin page
# =====================================================================================
set -u
cd "$(dirname "$0")"
bold(){ printf "\033[1m%s\033[0m\n" "$*"; }
ok(){ printf "\033[32m✔ %s\033[0m\n" "$*"; }
warn(){ printf "\033[33m⚠ %s\033[0m\n" "$*"; }
die(){ printf "\033[31m✖ %s\033[0m\n" "$*"; echo; read -r -p "Press Enter to close…" _; exit 1; }

bold "══════════════════════════════════════════════"
bold "  Face Attendance — install & run"
bold "══════════════════════════════════════════════"
ARCH=$(uname -m); echo "Mac: $ARCH, macOS $(sw_vers -productVersion)"

# ---- 1. LAN IP into Caddyfile ----
IP=""
for IF in $(networksetup -listallhardwareports 2>/dev/null | awk '/Device/{print $2}'); do
  IP=$(ipconfig getifaddr "$IF" 2>/dev/null) && [[ -n "$IP" ]] && break
done
if [[ -n "$IP" ]]; then
  sed -i '' -E "s#^https://[0-9.]+(:[0-9]+)? \{#https://$IP:8443 {#" Caddyfile
  sed -i '' -E "s#^([[:space:]]*default_sni )[0-9.]+#\\1$IP#" Caddyfile
  ok "LAN IP: $IP (written to Caddyfile)"
else
  warn "Could not detect a LAN IP; iPads on the network may not reach this Mac until it has one."
fi

# ---- 2. Docker ----
export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin:$HOME/Applications/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin"
if ! command -v docker >/dev/null 2>&1; then
  bold "Docker Desktop not found — downloading the official Apple Silicon build (~600 MB)…"
  DMG="$HOME/Downloads/Docker.dmg"
  [[ "$ARCH" == "arm64" ]] && URL="https://desktop.docker.com/mac/main/arm64/Docker.dmg" || URL="https://desktop.docker.com/mac/main/amd64/Docker.dmg"
  mkdir -p "$HOME/Downloads"
  curl -L --progress-bar -o "$DMG" "$URL" || die "Download failed — check the internet connection and re-run."
  MNT=$(hdiutil attach -nobrowse -readonly "$DMG" | awk -F'\t' '/Volumes/{print $NF}' | tail -1)
  [[ -d "$MNT/Docker.app" ]] || die "Could not open Docker.dmg"
  DEST=/Applications; [[ -w /Applications ]] || DEST="$HOME/Applications"; mkdir -p "$DEST"
  echo "Installing to $DEST/Docker.app…"; rm -rf "$DEST/Docker.app"; cp -R "$MNT/Docker.app" "$DEST/" || die "Copy to $DEST failed"
  hdiutil detach "$MNT" -quiet || true
  export PATH="$PATH:$DEST/Docker.app/Contents/Resources/bin"
  ok "Docker Desktop installed in $DEST"
fi

if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop… (first launch: accept its welcome screen, skip sign-in)"
  open -a Docker
  for i in $(seq 1 90); do docker info >/dev/null 2>&1 && break; sleep 3; done
  docker info >/dev/null 2>&1 || die "Docker did not start in time. Open Docker Desktop, finish its first-run screen, then re-run this file."
fi
ok "Docker is running ($(docker --version))"

# ---- 3. Build + start ----
bold "Building + starting containers (first build downloads the face models, ~2–4 min)…"
docker compose up -d --build --remove-orphans || die "docker compose failed — scroll up for the error."

echo -n "Waiting for the recognizer to be ready "
for i in $(seq 1 90); do
  curl -s http://localhost:8181/api/health 2>/dev/null | grep -q '"ok": *true' && break
  echo -n "."; sleep 2
done; echo
curl -s http://localhost:8181/api/health | grep -q '"ok": *true' && ok "Recognizer connected" || warn "Recognizer not confirmed yet — check: docker compose logs recognizer"

# ---- 4. Done ----
PIN=$(awk -F= '/^ADMIN_PIN/{print $2}' .env 2>/dev/null); PIN=${PIN:-2468}
echo; bold "── Face Attendance is running ─────────────────────"
echo "Admin (this Mac):  http://localhost:8181/admin      PIN: $PIN"
[[ -n "$IP" ]] && echo "Admin (iPads):     https://$IP:8443/admin"
[[ -n "$IP" ]] && echo "Kiosk (iPads):     https://$IP:8443/?kiosk=Floor-1"
echo "Health:            curl http://localhost:8181/api/health"
echo
echo "Enrol staff in the Admin → Enrol tab (needs a face in front of the camera)."
open "http://localhost:8181/admin" >/dev/null 2>&1 || true
read -r -p "Press Enter to close…" _
