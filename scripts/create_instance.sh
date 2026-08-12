#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Create the WhatsApp instance in Evolution API and connect via QR code.
# Run this ONCE after the stack is up and before sending any messages.
#
# Usage:
#   chmod +x scripts/create_instance.sh
#   ./scripts/create_instance.sh
#
# Reads values from .env in the project root.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env
if [[ -f "$ROOT_DIR/.env" ]]; then
  export $(grep -v '^#' "$ROOT_DIR/.env" | xargs)
fi

EVOLUTION_API_URL="${EVOLUTION_SERVER_URL:-http://localhost:5020}"
API_KEY="${EVOLUTION_API_KEY:?EVOLUTION_API_KEY not set}"
INSTANCE="${INSTANCE_NAME:-whatsapp-agent}"
WEBHOOK="${WEBHOOK_URL:-http://bridge:5021/webhook}"

echo "==> Evolution API: $EVOLUTION_API_URL"
echo "==> Instance name: $INSTANCE"
echo ""

# ---- 1. Create instance -----------------------------------------------
echo "[1/3] Creating instance '$INSTANCE'..."
curl -s -X POST "$EVOLUTION_API_URL/instance/create" \
  -H "apikey: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"instanceName\": \"$INSTANCE\",
    \"qrcode\": true,
    \"integration\": \"WHATSAPP-BAILEYS\"
  }" | python3 -m json.tool || true

echo ""

# ---- 2. Register webhook -----------------------------------------------
echo "[2/3] Registering webhook..."
curl -s -X POST "$EVOLUTION_API_URL/webhook/set/$INSTANCE" \
  -H "apikey: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"$WEBHOOK\",
    \"webhook_by_events\": false,
    \"webhook_base64\": false,
    \"events\": [\"MESSAGES_UPSERT\", \"CONNECTION_UPDATE\", \"QRCODE_UPDATED\"]
  }" | python3 -m json.tool || true

echo ""

# ---- 3. Fetch QR code --------------------------------------------------
echo "[3/3] Fetching QR code (scan this with WhatsApp)..."
curl -s "$EVOLUTION_API_URL/instance/connect/$INSTANCE" \
  -H "apikey: $API_KEY" | python3 -m json.tool || true

echo ""
echo "Done. Open the Evolution API manager at $EVOLUTION_API_URL/manager"
echo "to scan the QR code with your WhatsApp mobile app."
