#!/usr/bin/env bash
set -euo pipefail

INITIAL_CAPITAL="${INITIAL_CAPITAL:-8000}"
STRAT_CONFIG="${STRAT_CONFIG:-$HOME/strat_config.ini}"

echo "Pulling latest code..."
git pull origin main

echo "Ensuring host directories exist..."
mkdir -p state logs

if [ ! -f state/equity.json ]; then
    echo "{\"equity\": ${INITIAL_CAPITAL}.0}" > state/equity.json
    echo "Initialized equity.json with \$${INITIAL_CAPITAL}"
fi

# ── Auto-generate .env from strat_config.ini ──
if [ -f "$STRAT_CONFIG" ]; then
    echo "Generating .env from $STRAT_CONFIG ..."
    python3 - "$STRAT_CONFIG" << 'PYEOF'
import configparser, sys
cfg = configparser.ConfigParser()
cfg.read(sys.argv[1])
lines = []
if cfg.has_section("derive"):
    lines.append(f"DERIVE_ENV={cfg.get('derive', 'env', fallback='PROD')}")
    lines.append(f"DERIVE_WALLET={cfg.get('derive', 'wallet', fallback='')}")
    lines.append(f"DERIVE_SESSION_KEY={cfg.get('derive', 'session_key', fallback='')}")
    lines.append(f"DERIVE_SUBACCOUNT_ID={cfg.get('derive', 'subaccount_id', fallback='0')}")
lines.append("DRY_RUN=false")
if cfg.has_section("telegram"):
    lines.append(f"TELEGRAM_BOT_TOKEN={cfg.get('telegram', 'ops_bot_token', fallback=cfg.get('telegram', 'bot_token', fallback=''))}")
    lines.append(f"TELEGRAM_CHAT_ID={cfg.get('telegram', 'ops_chat_id', fallback=cfg.get('telegram', 'chat_id', fallback=''))}")
    lines.append(f"TELEGRAM_REPORT_BOT_TOKEN={cfg.get('telegram', 'bot_token', fallback='')}")
    lines.append(f"TELEGRAM_REPORT_CHAT_ID={cfg.get('telegram', 'chat_id', fallback='')}")
lines.append("LOG_LEVEL=INFO")
with open(".env", "w") as f:
    f.write("\n".join(lines) + "\n")
print("  .env written OK")
PYEOF
elif [ ! -f .env ]; then
    echo "ERROR: No strat_config.ini at $STRAT_CONFIG and no .env file found."
    echo "       Copy .env.example and fill in your credentials."
    exit 1
fi

echo "Building and starting container..."
sudo docker compose up -d --build

echo "Done. Container status:"
sudo docker compose ps

echo ""
echo "View logs: sudo docker compose logs -f algo"
