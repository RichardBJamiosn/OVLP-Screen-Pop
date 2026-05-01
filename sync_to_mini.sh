#!/bin/bash
# Sync OVLP Screen Pop v2.0 to Mac Mini vault
# Run when Mini (192.168.0.107) is back online

MINI="macbook@192.168.0.107"
MINI_VAULT="/Volumes/richardjamison/Documents/Projects/OVLP Pop"
SSH_KEY="$HOME/.ssh/id_ed25519"

echo "Checking Mini connectivity..."
if ! ssh -i "$SSH_KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$MINI" "echo ok" &>/dev/null; then
  echo "Mini offline — try again later"
  exit 1
fi

echo "Mini online. Syncing..."
ssh -i "$SSH_KEY" "$MINI" "mkdir -p '$MINI_VAULT/v2.0-richard' '$MINI_VAULT/v2.0-kehlen'"

# Push Richard's files
scp -i "$SSH_KEY" \
  ~/ovlp-pop/server.py \
  ~/ovlp-pop/dashboard.html \
  ~/ovlp-pop/stress_test.py \
  ~/ovlp-pop/.gitignore \
  "$MINI:$MINI_VAULT/v2.0-richard/"

# Push Kehlen's files (already pulled locally to vault)
VAULT="$HOME/Documents/New BIg Boy/iCloud/REAL ESTATE/Tools/OVLP Screen Pop"
scp -i "$SSH_KEY" \
  "$VAULT/v2.0-kehlen/server.py" \
  "$VAULT/v2.0-kehlen/dashboard.html" \
  "$MINI:$MINI_VAULT/v2.0-kehlen/"

# Copy install notes
scp -i "$SSH_KEY" "$VAULT/INSTALL.md" "$MINI:$MINI_VAULT/"

echo "Done. Mini vault synced at: $MINI_VAULT"
