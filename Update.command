#!/bin/bash
# OVLP Screen Pop — One-Click Updater
# Double-click this file to update to the latest version

clear
echo "================================================"
echo "   OVLP Screen Pop — Updater"
echo "================================================"
echo ""

INSTALL_DIR="$HOME/ovlp-pop"
BASE_URL="https://raw.githubusercontent.com/RichardBJamiosn/OVLP-Screen-Pop/main"

# Check install folder exists
if [ ! -d "$INSTALL_DIR" ]; then
  echo "  ERROR: No OVLP install found at $INSTALL_DIR"
  echo "  Contact Richard for a fresh install."
  echo ""
  read -p "  Press Enter to close..."
  exit 1
fi

echo "  Found install at: $INSTALL_DIR"
echo "  Downloading latest version..."
echo ""

# Download new files
curl -s "$BASE_URL/server.py" -o "$INSTALL_DIR/server.py"
if [ $? -ne 0 ]; then
  echo "  ERROR: Download failed. Check your internet connection."
  read -p "  Press Enter to close..."
  exit 1
fi

curl -s "$BASE_URL/dashboard.html" -o "$INSTALL_DIR/dashboard.html"

echo "  ✓ Files updated"
echo ""
echo "  Restarting server..."

# Kill old server
lsof -ti :5050 | xargs kill -9 2>/dev/null
sleep 1

# Start new server
cd "$INSTALL_DIR"
nohup python3 server.py >> server.log 2>&1 &
sleep 3

# Verify
STATUS=$(curl -s http://localhost:5050/ping 2>/dev/null)
if echo "$STATUS" | grep -q "running"; then
  echo "  ✓ Server running"
  echo ""
  echo "================================================"
  echo "   Update complete — you are on v2.0"
  echo "================================================"
  echo ""
  echo "  Hard refresh your browser to see the changes:"
  echo "  Mac: Cmd + Shift + R"
  echo ""
else
  echo "  WARNING: Server may not have started."
  echo "  Try double-clicking Start.command"
  echo ""
fi

read -p "  Press Enter to close..."
