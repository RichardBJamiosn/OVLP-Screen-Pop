#!/bin/bash
# OVLP Screen Pop — Remote Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/RichardBJamiosn/OVLP-Screen-Pop/main/install.sh | bash
#
# No files to download manually = no macOS quarantine = no Gatekeeper blocks.
# Works on Big Sur (11.x) through Sequoia (15.x).

set -e

INSTALL_DIR="$HOME/ovlp-pop"
REPO_RAW="https://raw.githubusercontent.com/RichardBJamiosn/OVLP-Screen-Pop/main"
PLIST_LABEL="com.ovlp.screenpop"
PLIST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

clear
echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║      OVLP Screen Pop — Remote Install            ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""

# ── Check Python 3 ────────────────────────────────────────
echo "  [1/6] Checking Python 3..."
PYTHON=""
for candidate in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if "$candidate" --version >/dev/null 2>&1; then
        version=$("$candidate" --version 2>&1)
        if echo "$version" | grep -q "Python 3"; then
            PYTHON="$candidate"
            echo "  ✓ $version"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    echo "  ✗ Python 3 not found."
    echo ""
    echo "  Fix: Open Safari and go to https://www.python.org/downloads"
    echo "  Install Python 3, then run this command again."
    echo ""
    exit 1
fi

# ── Fix/bootstrap pip ─────────────────────────────────────
echo "  [2/6] Checking pip..."
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "  pip missing — bootstrapping..."
    PIP_URL="https://bootstrap.pypa.io/pip/${PY_VER}/get-pip.py"
    curl -fsSL "$PIP_URL" -o /tmp/get-pip.py 2>/dev/null || \
        curl -fsSL "https://bootstrap.pypa.io/get-pip.py" -o /tmp/get-pip.py
    "$PYTHON" /tmp/get-pip.py --user --quiet 2>&1 | tail -3
fi

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo ""
    echo "  ✗ Could not set up pip."
    echo "  Send Richard a screenshot of this window."
    exit 1
fi
echo "  ✓ pip ready"

# ── Install dependencies ──────────────────────────────────
echo "  [3/6] Installing dependencies..."
"$PYTHON" -m pip install flask certifi requests --user --quiet 2>&1 | grep -iE "error|already" | head -3

if ! "$PYTHON" -c "import flask" 2>/dev/null; then
    echo ""
    echo "  ✗ Flask failed to install."
    echo "  Send Richard a screenshot of this window."
    exit 1
fi
echo "  ✓ Flask, certifi, requests installed"
echo ""

# ── Pick agent (env var OVLP_AGENT overrides prompt) ──────
if [ -n "$OVLP_AGENT" ]; then
    case $OVLP_AGENT in
        richard) AGENT_NAME="Richard"; AGENT_SLUG="richard" ;;
        oj)      AGENT_NAME="OJ";      AGENT_SLUG="oj"      ;;
        kehlen)  AGENT_NAME="Kehlen";  AGENT_SLUG="kehlen"  ;;
        lucia)   AGENT_NAME="Lucia";   AGENT_SLUG="lucia"   ;;
        *)       echo "  ✗ Unknown agent: $OVLP_AGENT"; exit 1 ;;
    esac
    echo "  [4/6] Agent: $AGENT_NAME"
else
    echo "  [4/6] Who are you? Pick your name:"
    echo ""
    echo "    1)  Richard"
    echo "    2)  OJ"
    echo "    3)  Kehlen"
    echo "    4)  Lucia"
    echo ""
    read -p "  Enter 1-4: " CHOICE
    case $CHOICE in
        1) AGENT_NAME="Richard"; AGENT_SLUG="richard" ;;
        2) AGENT_NAME="OJ";      AGENT_SLUG="oj"      ;;
        3) AGENT_NAME="Kehlen";  AGENT_SLUG="kehlen"  ;;
        4) AGENT_NAME="Lucia";   AGENT_SLUG="lucia"   ;;
        *)
            echo "  Invalid choice. Run the command again."
            exit 1 ;;
    esac
fi
echo "  ✓ Agent: $AGENT_NAME"

# ── GHL API Key (env var OVLP_KEY overrides prompt) ───────
if [ -n "$OVLP_KEY" ]; then
    GHL_KEY="$OVLP_KEY"
    echo "  [4b/6] API key: pre-configured ✓"
else
    echo ""
    echo "  Enter the GHL API key (get from Richard):"
    read -p "  Key: " GHL_KEY
    if [ -z "$GHL_KEY" ]; then
        echo "  ✗ Key cannot be blank."
        exit 1
    fi
fi
echo "  ✓ Key saved"

# ── Download files from GitHub ────────────────────────────
echo ""
echo "  [5/6] Downloading from GitHub..."
mkdir -p "$INSTALL_DIR"

curl -fsSL "${REPO_RAW}/server.py"      -o "$INSTALL_DIR/server.py"
curl -fsSL "${REPO_RAW}/dashboard.html" -o "$INSTALL_DIR/dashboard.html"

# Remove quarantine flag just in case (belt and suspenders)
xattr -dr com.apple.quarantine "$INSTALL_DIR" 2>/dev/null || true

echo "  ✓ server.py downloaded"
echo "  ✓ dashboard.html downloaded"

# ── Write config ──────────────────────────────────────────
cat > "$INSTALL_DIR/ovlp_config.json" << EOF
{
    "agent_name": "$AGENT_SLUG",
    "ghl_api_key": "$GHL_KEY",
    "server_url": "http://localhost:5050",
    "ghl_location_id": "bNT4wp0nukIQdBJbQDaa",
    "agents": [
        {"name": "$AGENT_NAME", "slug": "$AGENT_SLUG", "role": "employee"}
    ]
}
EOF
echo "  ✓ Config written"

# ── Set up launchd (auto-start on login) ──────────────────
echo ""
echo "  [6/6] Setting up auto-start..."
mkdir -p "$HOME/Library/LaunchAgents"

# Stop any existing version
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null || \
    launchctl unload "$PLIST" 2>/dev/null || true

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${INSTALL_DIR}/server.py</string>
    </array>
    <key>WorkingDirectory</key><string>${INSTALL_DIR}</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/tmp/ovlp_server.log</string>
    <key>StandardErrorPath</key><string>/tmp/ovlp_server.log</string>
</dict>
</plist>
EOF

launchctl bootstrap gui/$(id -u) "$PLIST" 2>/dev/null || \
    launchctl load "$PLIST" 2>/dev/null || true
echo "  ✓ Auto-start enabled"

# ── Wait for server ───────────────────────────────────────
echo ""
echo "  Starting server..."
for i in $(seq 1 15); do
    if curl -s http://localhost:5050/ping >/dev/null 2>&1; then
        echo "  ✓ Server running!"
        break
    fi
    sleep 1
    if [ "$i" -eq 15 ]; then
        echo "  ⚠ Server slow to start. Check /tmp/ovlp_server.log"
    fi
done

# ── Done ──────────────────────────────────────────────────
echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║  ✓ Setup complete!                               ║"
echo "  ║                                                  ║"
echo "  ║  Dashboard: http://localhost:5050/?agent=$AGENT_SLUG"
echo "  ║  Bookmarklet: http://localhost:5050/setup        ║"
echo "  ║                                                  ║"
echo "  ║  Starts automatically when you log in.           ║"
echo "  ║  Updates automatically from GitHub.              ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""

sleep 1
open "http://localhost:5050/?agent=$AGENT_SLUG"
open "http://localhost:5050/setup"

echo "  You can close this Terminal window now."
echo ""
