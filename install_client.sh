#!/bin/bash
set -e

# Crebain Client Installer
# Installs the Crebain monitoring client and configures a systemd service.

INSTALL_DIR="/opt/crebain-client"
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
echo -e "${CYAN}  Crebain Client Installer${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# --- Prompt for server destination hash ---
# This is the only input we absolutely need from the user.
if [ -n "$1" ]; then
    SERVER_HASH="$1"
    info "Using server monitoring hash from argument: $SERVER_HASH"
else
    echo -e "${YELLOW}Enter the server's Monitoring destination hash${NC}"
    echo -e "(shown at the end of install_server.sh output):"
    read -rp "> " SERVER_HASH
fi

if [ -z "$SERVER_HASH" ] || ! [[ "$SERVER_HASH" =~ ^[a-f0-9]{32}$ ]]; then
    err "Invalid destination hash. Must be a 32-character hex string."
    err "Run 'journalctl -u crebain-server | grep destination' on the server to find it."
    exit 1
fi

# Proxy hash is optional - prompt only if user wants it
echo ""
echo -e "Enter the server's ${YELLOW}Proxy destination hash${NC} (leave blank to skip proxy):"
read -rp "> " PROXY_HASH

if [ -n "$PROXY_HASH" ] && ! [[ "$PROXY_HASH" =~ ^[a-f0-9]{32}$ ]]; then
    warn "Invalid proxy hash format, disabling proxy."
    PROXY_HASH=""
fi

# Generate a unique node ID from hostname
NODE_ID="$(hostname | tr '[:upper:]' '[:lower:]' | tr ' ' '_')"
info "Using node_id: $NODE_ID (derived from hostname)"

# --- Install system dependencies ---
info "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip > /dev/null 2>&1
ok "System dependencies installed."

# --- Create service user ---
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
cp "$SCRIPT_DIR/client.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

if [ -f "$SCRIPT_DIR/client.conf.example" ]; then
    cp "$SCRIPT_DIR/client.conf.example" "$INSTALL_DIR/"
fi
ok "Application files copied."

# --- Create Python virtual environment ---
info "Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip

# Client only needs core + optional GPS dependencies
"$VENV_DIR/bin/pip" install --quiet "RNS>=0.4.0" "psutil>=5.8.0" "gpsd-py3>=0.3.0" 2>/dev/null || \
"$VENV_DIR/bin/pip" install --quiet "RNS>=0.4.0" "psutil>=5.8.0"
ok "Python dependencies installed."

# --- Generate client.conf ---
if [ ! -f "$INSTALL_DIR/client.conf" ]; then
    info "Generating client.conf..."

    PROXY_ENABLED="false"
    PROXY_SECTION="[proxy]
enabled = false
port = 8080"

    if [ -n "$PROXY_HASH" ]; then
        PROXY_ENABLED="true"
        PROXY_SECTION="[proxy]
enabled = true
port = 8080
timeout = 15"
    fi

    cat > "$INSTALL_DIR/client.conf" <<EOF
[node]
node_id = $NODE_ID
identity_path = $INSTALL_DIR/node_identity

[server]
destination = $SERVER_HASH
$([ -n "$PROXY_HASH" ] && echo "proxy_destination = $PROXY_HASH")

[monitoring]
interval = 60
services = sshd,systemd-resolved,NetworkManager

[rns]
packet_size_limit = 400
link_packet_size = 1000
link_timeout = 30

[data]
collect_gps = true
collect_services = true
collect_storage = true
collect_network = true
collect_users = true
collect_power = true
collect_system = true
collect_rns = true

$PROXY_SECTION
EOF
    ok "client.conf created with node_id=$NODE_ID"
else
    ok "client.conf already exists, preserving."
fi

# --- Set up Reticulum config if not present ---
RETIC_DIR="/home/$SUDO_USER/.reticulum"
if [ -n "$SUDO_USER" ] && [ -d "$RETIC_DIR" ]; then
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

# --- Create systemd service ---
info "Creating systemd service..."

cat > /etc/systemd/system/crebain-client.service <<EOF
[Unit]
Description=Crebain RNS Monitoring Client
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="HOME=$INSTALL_DIR"
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/client.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

ok "Systemd service created."

# --- Enable and start service ---
systemctl daemon-reload
systemctl enable crebain-client.service
systemctl start crebain-client.service

ok "Service started."

# --- Done ---
echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Installation Complete${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""
echo "  Node ID:         $NODE_ID"
echo "  Server hash:     $SERVER_HASH"
[ -n "$PROXY_HASH" ] && echo "  Proxy hash:      $PROXY_HASH"
echo "  Install dir:     $INSTALL_DIR"
echo "  Config:          $INSTALL_DIR/client.conf"
echo ""
echo "Useful commands:"
echo "  systemctl status crebain-client"
echo "  journalctl -fu crebain-client"
echo ""

if [ ! -f "$INSTALL_DIR/.reticulum/config" ]; then
    warn "Reticulum config not yet generated. It will be created on first RNS init."
    warn "After first run, edit: $INSTALL_DIR/.reticulum/config"
    warn "Then restart: systemctl restart crebain-client"
fi

echo -e "${GREEN}Done.${NC}"
