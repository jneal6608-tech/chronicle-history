#!/usr/bin/env bash
# Chronicle Shorts - one-step installer for Chromebooks (ChromeOS Linux development environment).
# Run once from the chronicle-shorts folder:   bash install.sh
set -u
cd "$(dirname "$(readlink -f "$0")")"
DIR="$(pwd)"
say()  { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mSetup stopped: %s\033[0m\n' "$*"; exit 1; }

say "Chronicle Shorts setup for Chromebook"
if [ -e /opt/google/cros-containers ] || [ -e /dev/.cros_milestone ]; then
  echo "ChromeOS Linux environment detected."
else
  echo "Note: this doesn't look like a Chromebook's Linux environment. Continuing anyway."
fi
echo "Processor type: $(uname -m)"

# --- Disk space: the Linux disk holds packages (~1 GB), the speech model and your uploads.
FREE_GB=$(df -Pk "$HOME" | awk 'NR==2 {printf "%d", $4/1048576}')
echo "Free space in Linux: ${FREE_GB} GB"
if [ "$FREE_GB" -lt 4 ]; then
  fail "Linux has only ${FREE_GB} GB free. Make the Linux disk bigger: Settings > About ChromeOS > Developers > Linux development environment > Disk size (20 GB or more), then run this again."
fi
[ "$FREE_GB" -lt 12 ] && echo "Tip: with under 12 GB free, long documentaries may not fit. You can enlarge the Linux disk in Settings > About ChromeOS > Developers."

# --- System packages
say "Installing Python and FFmpeg (you may see a lot of text - that's normal)"
sudo apt-get update || fail "Could not reach the package servers. Check the Chromebook's internet connection and try again."
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg curl \
  || fail "Package installation failed. Run 'sudo apt-get update' and try again."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python $(python3 --version) is too old; 3.10 or newer is needed. Update the Linux environment: sudo apt-get update && sudo apt-get full-upgrade"
echo "Python: $(python3 --version)   FFmpeg: $(ffmpeg -version | head -1 | cut -d' ' -f3)"

# --- Python environment
say "Installing the Chronicle Shorts engine"
[ -x .venv/bin/python ] || python3 -m venv .venv || fail "Could not create the Python environment."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install --prefer-binary -r requirements.txt \
  || fail "Installing the Python packages failed (see the messages above). Check the internet connection and run 'bash install.sh' again."

[ -f .env ] || cp .env.example .env
mkdir -p data

# --- Speech model (one-time download from huggingface.co)
say "Downloading the speech-recognition model (one time, about 150 MB)"
.venv/bin/python - <<'PY'
from app import transcribe
try:
    transcribe.load_model()
    print("Speech model ready.")
except Exception:
    print("The speech model couldn't be downloaded right now. That's OK - it downloads "
          "automatically the first time you process a video (internet needed then).")
PY

# --- ChromeOS launcher icons
say "Adding Chronicle Shorts to your Chromebook's launcher"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$APPS" "$ICONS"
cp app/assets/icon.png "$ICONS/chronicle-shorts.png"
cat > "$APPS/chronicle-shorts.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Chronicle Shorts
Comment=Turn a documentary into finished YouTube Shorts, TikToks and Reels
Exec=/bin/bash "$DIR/start.sh"
Icon=chronicle-shorts
Terminal=false
Categories=AudioVideo;Video;
DESK
cat > "$APPS/chronicle-shorts-stop.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Stop Chronicle Shorts
Comment=Stop the Chronicle Shorts engine (unfinished jobs can be resumed later)
Exec=/bin/bash "$DIR/stop.sh"
Icon=chronicle-shorts
Terminal=false
Categories=AudioVideo;Video;
DESK
chmod +x start.sh stop.sh install.sh
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1

say "Setup complete"
echo "Open the launcher (the circle button, bottom-left) and click 'Chronicle Shorts'."
echo "It may be inside the 'Linux apps' folder. Starting it for you now..."
bash "$DIR/start.sh"
