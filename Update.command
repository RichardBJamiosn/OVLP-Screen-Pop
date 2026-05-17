#!/bin/bash
# OVLP Screen Pop — One-Click Updater
# Double-click this file to update. Works at the office AND from home.

clear
echo "================================================"
echo "   OVLP Screen Pop — Updater"
echo "================================================"
echo ""

INSTALL_DIR="$HOME/ovlp-pop"
LAN_SERVER="http://192.168.0.103:5050"
GITHUB_BASE="https://raw.githubusercontent.com/RichardBJamiosn/OVLP-Screen-Pop/main"

# Remove macOS quarantine so it runs without security warnings
xattr -d com.apple.quarantine "$0" 2>/dev/null

if [ ! -d "$INSTALL_DIR" ]; then
  echo "  ERROR: No OVLP install found at $INSTALL_DIR"
  echo "  Contact Richard for a fresh install."
  read -p "  Press Enter to close..."
  exit 1
fi

echo "  Found install at: $INSTALL_DIR"
echo ""

# ── Try LAN first (office), fall back to GitHub (home) ──────────────────────
BASE_URL=""
SOURCE_NAME=""

echo "  Checking office server..."
LAN_CHECK=$(curl -s --max-time 4 "$LAN_SERVER/update/version" 2>/dev/null)
if echo "$LAN_CHECK" | grep -q "version"; then
  BASE_URL="$LAN_SERVER/update"
  SOURCE_NAME="office server"
  REMOTE_VER=$(echo "$LAN_CHECK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null)
  echo "  ✓ Office server reachable — v$REMOTE_VER available"
else
  echo "  Office not reachable — trying GitHub..."
  GH_CHECK=$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" "$GITHUB_BASE/server.py" 2>/dev/null)
  if [ "$GH_CHECK" = "200" ]; then
    BASE_URL="$GITHUB_BASE"
    SOURCE_NAME="GitHub"
    REMOTE_VER="latest"
    echo "  ✓ GitHub reachable"
  else
    echo ""
    echo "  ERROR: Cannot reach office server or GitHub."
    echo "  Check your internet connection and try again."
    read -p "  Press Enter to close..."
    exit 1
  fi
fi

echo ""
echo "  Downloading from $SOURCE_NAME..."

curl -s --max-time 30 "$BASE_URL/server.py" -o "$INSTALL_DIR/server.py"
if [ $? -ne 0 ]; then
  echo "  ERROR: Download of server.py failed."
  read -p "  Press Enter to close..."
  exit 1
fi

curl -s --max-time 30 "$BASE_URL/dashboard.html" -o "$INSTALL_DIR/dashboard.html"
if [ $? -ne 0 ]; then
  echo "  ERROR: Download of dashboard.html failed."
  read -p "  Press Enter to close..."
  exit 1
fi

echo "  ✓ Files updated"
echo ""
echo "  Restarting server..."

lsof -ti :5050 | xargs kill -9 2>/dev/null
sleep 1

cd "$INSTALL_DIR"
nohup python3 server.py >> server.log 2>&1 &
sleep 3

STATUS=$(curl -s http://localhost:5050/ping 2>/dev/null)
if echo "$STATUS" | grep -q "running"; then
  echo "  ✓ Server running"
  echo ""
  echo "================================================"
  echo "   Update complete!"
  echo "================================================"
  echo ""
  echo "  Hard refresh your browser: Cmd + Shift + R"
  echo ""
else
  echo "  WARNING: Server may not have started. Check server.log"
  echo ""
fi

read -p "  Press Enter to close..."
