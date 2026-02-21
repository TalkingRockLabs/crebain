#!/bin/bash
set -e

# Crebain Server Installer
# Installs the Crebain monitoring server, web UI, and configures systemd services.

INSTALL_DIR="/opt/crebain-server"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_USER="crebain"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# --- Pre-flight checks ---
if [ "$EUID" -ne 0 ]; then
    err "This script must be run as root (use sudo)."
    exit 1
fi

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Crebain Server Installer${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# --- Install system dependencies ---
info "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip > /dev/null 2>&1
ok "System dependencies installed."

# --- Create service user (no login shell, no home dir interaction) ---
if id "$SERVICE_USER" &>/dev/null; then
    ok "Service user '$SERVICE_USER' already exists."
else
    info "Creating service user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Service user created."
fi

# --- Copy application files ---
info "Installing application to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/server.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/app.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/templates" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/static" "$INSTALL_DIR/"

if [ -f "$SCRIPT_DIR/server.conf.example" ]; then
    cp "$SCRIPT_DIR/server.conf.example" "$INSTALL_DIR/"
fi
ok "Application files copied."

# --- Create Python virtual environment ---
info "Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Python dependencies installed."

# --- Generate server.conf ---
if [ ! -f "$INSTALL_DIR/server.conf" ]; then
    info "Generating server.conf..."
    cat > "$INSTALL_DIR/server.conf" <<EOF
[server]
identity_path = $INSTALL_DIR/server_identity
announce_interval = 30

[database]
path = $INSTALL_DIR/node_monitoring.db
retention_days = 30

[logging]
level = INFO
EOF
    ok "server.conf created."
else
    ok "server.conf already exists, preserving."
fi

# --- Set up Reticulum config if not present ---
RETIC_DIR="/home/$SUDO_USER/.reticulum"
if [ -n "$SUDO_USER" ] && [ -d "$RETIC_DIR" ]; then
    # Reticulum config already exists from the invoking user, copy it for the service user
    SERVICE_RETIC_DIR="$INSTALL_DIR/.reticulum"
    if [ ! -d "$SERVICE_RETIC_DIR" ]; then
        info "Copying Reticulum config from $RETIC_DIR..."
        cp -r "$RETIC_DIR" "$SERVICE_RETIC_DIR"
        ok "Reticulum config copied."
    else
        ok "Reticulum config already present at $SERVICE_RETIC_DIR."
    fi
elif [ ! -d "$INSTALL_DIR/.reticulum" ]; then
    warn "No existing Reticulum config found."
    warn "A default config will be generated on first run."
    warn "You may need to edit $INSTALL_DIR/.reticulum/config to configure your RNS interface (e.g., LoRa RNode)."
fi

# --- Set ownership ---
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# If there's an RNode device, give the service user access to dialout group
if [ -e /dev/ttyUSB0 ] || [ -e /dev/ttyACM0 ]; then
    info "Serial device detected, adding $SERVICE_USER to dialout group..."
    usermod -aG dialout "$SERVICE_USER"
    ok "Added to dialout group."
fi

# --- Create systemd service for server.py ---
info "Creating systemd services..."

cat > /etc/systemd/system/crebain-server.service <<EOF
[Unit]
Description=Crebain RNS Monitoring Server
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="HOME=$INSTALL_DIR"
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# --- Create systemd service for app.py (web UI) ---
cat > /etc/systemd/system/crebain-web.service <<EOF
[Unit]
Description=Crebain Web UI
After=network.target crebain-server.service
Requires=crebain-server.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="HOME=$INSTALL_DIR"
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/app.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

ok "Systemd services created."

# --- Enable and start services ---
systemctl daemon-reload
systemctl enable crebain-server.service
systemctl enable crebain-web.service
systemctl start crebain-server.service

# Give the server a moment to initialize RNS and generate identity
info "Waiting for server to initialize and generate RNS identity..."
sleep 5

systemctl start crebain-web.service

ok "Services started."

# --- Extract and display destination hashes ---
echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Installation Complete${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# Read destination hashes from server logs
MONITORING_HASH=$(journalctl -u crebain-server.service --no-pager -n 50 2>/dev/null | grep -oP 'Monitoring destination: \K[a-f0-9]+' | tail -1)
PROXY_HASH=$(journalctl -u crebain-server.service --no-pager -n 50 2>/dev/null | grep -oP 'Proxy destination: \K[a-f0-9]+' | tail -1)

if [ -n "$MONITORING_HASH" ] && [ -n "$PROXY_HASH" ]; then
    echo -e "${GREEN}Server RNS Destination Hashes:${NC}"
    echo -e "  Monitoring: ${YELLOW}${MONITORING_HASH}${NC}"
    echo -e "  Proxy:      ${YELLOW}${PROXY_HASH}${NC}"
    echo ""
    echo -e "${CYAN}You will need these hashes when installing clients.${NC}"
else
    warn "Could not read destination hashes from logs yet."
    warn "Run the following to retrieve them once the server is fully running:"
    echo ""
    echo "  journalctl -u crebain-server.service | grep 'destination:'"
    echo ""
fi

echo ""
echo "Services:"
echo "  crebain-server  - RNS monitoring server"
echo "  crebain-web     - Web UI (http://0.0.0.0:5000)"
echo ""
echo "Default web login:  admin / crebain"
echo ""
echo "Useful commands:"
echo "  systemctl status crebain-server"
echo "  systemctl status crebain-web"
echo "  journalctl -fu crebain-server"
echo "  journalctl -fu crebain-web"
echo ""

if [ ! -f "$INSTALL_DIR/.reticulum/config" ]; then
    warn "Reticulum config not yet generated. It will be created on first RNS init."
    warn "After first run, edit: $INSTALL_DIR/.reticulum/config"
    warn "Then restart: systemctl restart crebain-server"
fi

echo -e "${GREEN}Done.${NC}"
