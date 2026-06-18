#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/bat_node_system}"
APP_USER="${APP_USER:-$(id -un)}"
SERVER_PORT="${SERVER_PORT:-8000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
PI_HOSTNAME="${PI_HOSTNAME:-raspberrypi}"

SERVER_DIR="$APP_DIR/server"
DASHBOARD_DIR="$APP_DIR/dashboard"
SERVER_ENV="$SERVER_DIR/bat_server.env"
DASHBOARD_ENV="$DASHBOARD_DIR/bat_dashboard.env"

if [ ! -d "$SERVER_DIR" ] || [ ! -d "$DASHBOARD_DIR" ]; then
    echo "Expected server and dashboard folders under: $APP_DIR" >&2
    exit 1
fi

echo "Installing Raspberry Pi packages..."
sudo apt update
sudo apt install -y python3-venv python3-pip flac sqlite3 avahi-daemon

if [ "$(hostname)" != "$PI_HOSTNAME" ]; then
    echo "Setting Pi hostname to $PI_HOSTNAME..."
    sudo hostnamectl set-hostname "$PI_HOSTNAME"
fi
if grep -qE '^127\.0\.1\.1\s+' /etc/hosts; then
    sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$PI_HOSTNAME/" /etc/hosts
else
    echo -e "127.0.1.1\t$PI_HOSTNAME" | sudo tee -a /etc/hosts >/dev/null
fi
sudo systemctl enable --now avahi-daemon.service

echo "Creating server virtual environment..."
python3 -m venv "$SERVER_DIR/.venv"
"$SERVER_DIR/.venv/bin/python" -m pip install --upgrade pip
"$SERVER_DIR/.venv/bin/python" -m pip install -r "$SERVER_DIR/requirements.txt"

echo "Creating dashboard virtual environment..."
python3 -m venv "$DASHBOARD_DIR/.venv"
"$DASHBOARD_DIR/.venv/bin/python" -m pip install --upgrade pip
"$DASHBOARD_DIR/.venv/bin/python" -m pip install -r "$DASHBOARD_DIR/requirements_dashboard.txt"

mkdir -p "$SERVER_DIR/data"

if [ ! -f "$SERVER_ENV" ]; then
    PROVISIONING_TOKEN_VALUE="$("$SERVER_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    cat > "$SERVER_ENV" <<EOF
BAT_DB_PATH=$SERVER_DIR/bat_nodes_v2.db
BAT_DATA_DIR=$SERVER_DIR/data
AUTH_WINDOW_SECONDS=300
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=change-me-now
PROVISIONING_TOKEN=$PROVISIONING_TOKEN_VALUE
REQUIRE_FLAC_BEFORE_DELETE=0
REQUIRE_BACKUP_BEFORE_DELETE=0
FLAC_ENCODER=auto
FLAC_COMPRESSION_LEVEL=5
EOF
    chmod 600 "$SERVER_ENV"
else
    echo "Keeping existing server env: $SERVER_ENV"
fi

if [ ! -f "$DASHBOARD_ENV" ]; then
    cat > "$DASHBOARD_ENV" <<EOF
BAT_DB_PATH=$SERVER_DIR/bat_nodes_v2.db
BAT_DATA_DIR=$SERVER_DIR/data
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=change-me-now
EOF
    chmod 600 "$DASHBOARD_ENV"
else
    echo "Keeping existing dashboard env: $DASHBOARD_ENV"
fi

echo "Writing systemd service: bat-node-server"
sudo tee /etc/systemd/system/bat-node-server.service >/dev/null <<EOF
[Unit]
Description=Bat Node FastAPI ingest server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$SERVER_DIR
EnvironmentFile=$SERVER_ENV
ExecStart=$SERVER_DIR/.venv/bin/python -m uvicorn bat_server_runtime:app --host 0.0.0.0 --port $SERVER_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Writing systemd service: bat-node-dashboard"
sudo tee /etc/systemd/system/bat-node-dashboard.service >/dev/null <<EOF
[Unit]
Description=Bat Node Streamlit dashboard
Wants=network-online.target bat-node-server.service
After=network-online.target bat-node-server.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$DASHBOARD_DIR
EnvironmentFile=$DASHBOARD_ENV
ExecStart=$DASHBOARD_DIR/.venv/bin/streamlit run bat_dashboard_app.py --server.address 0.0.0.0 --server.port $DASHBOARD_PORT --server.headless true
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Enabling and starting services..."
sudo systemctl daemon-reload
sudo systemctl enable --now bat-node-server.service
sudo systemctl enable --now bat-node-dashboard.service

echo "Writing helper command: bat-node-info"
sudo tee /usr/local/bin/bat-node-info >/dev/null <<EOF
#!/usr/bin/env bash
set -euo pipefail

SERVER_ENV="$SERVER_ENV"
SERVER_DIR="$SERVER_DIR"
SERVER_PORT="$SERVER_PORT"
DASHBOARD_PORT="$DASHBOARD_PORT"

LAN_IP="\$(hostname -I | awk '{print \$1}')"
TOKEN=""
if [ -f "\$SERVER_ENV" ]; then
    TOKEN="\$(grep '^PROVISIONING_TOKEN=' "\$SERVER_ENV" | cut -d= -f2- || true)"
fi

echo "Bat Node Pi"
echo "==========="
echo "Hostname:       \$(hostname)"
echo "LAN IP:         \$LAN_IP"
echo "Server URL:     http://\$LAN_IP:\$SERVER_PORT"
echo "Server check:   http://\$LAN_IP:\$SERVER_PORT/v1/public/server_time"
echo "Dashboard:      http://\$LAN_IP:\$DASHBOARD_PORT"
echo "mDNS dashboard: http://\$(hostname).local:\$DASHBOARD_PORT"
echo
echo "ESP32 setup portal values"
echo "Server URL:     http://\$LAN_IP:\$SERVER_PORT"
echo "Token:          \$TOKEN"
echo
echo "Services"
systemctl --no-pager --plain --type=service --state=running | grep -E 'bat-node-(server|dashboard)' || true
echo
echo "Logs"
echo "  journalctl -u bat-node-server.service -f"
echo "  journalctl -u bat-node-dashboard.service -f"
echo
echo "Compress existing WAVs"
echo "  cd \$SERVER_DIR && ./.venv/bin/python compress_existing_wavs.py"
EOF
sudo chmod +x /usr/local/bin/bat-node-info

echo
echo "Setup complete."
echo "Server:    http://$(hostname -I | awk '{print $1}'):$SERVER_PORT/v1/public/server_time"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):$DASHBOARD_PORT"
echo "Hostname:  $PI_HOSTNAME.local"
echo "Provisioning token is stored in: $SERVER_ENV"
echo "Run 'bat-node-info' on the Pi to print setup URLs and token."
echo
echo "Check services:"
echo "  systemctl status bat-node-server.service"
echo "  systemctl status bat-node-dashboard.service"
echo
echo "Compress existing verified WAVs:"
echo "  cd $SERVER_DIR && ./.venv/bin/python compress_existing_wavs.py"
