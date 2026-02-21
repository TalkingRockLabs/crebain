#!/bin/bash
# Script to update client.conf with the proxy destination hash from server logs
#
# Usage: ./update_client_proxy_dest.sh

# Check if server is running
if ! pgrep -f "python.*server.py" > /dev/null; then
    echo "Error: server.py doesn't appear to be running"
    echo "Please start the server first with: python3 server.py"
    exit 1
fi

echo "Extracting proxy destination hash from server logs..."

# Give server a moment to log the destination
sleep 2

# Try to extract the proxy destination from recent logs (last 50 lines)
PROXY_DEST=$(journalctl -u rns-server --since "1 minute ago" -n 50 2>/dev/null | grep "Proxy destination:" | tail -1 | awk '{print $NF}')

# If journalctl doesn't work, try checking if server is running in terminal
if [ -z "$PROXY_DEST" ]; then
    echo "Could not find proxy destination in systemd logs."
    echo "Please check server output manually and look for:"
    echo "  Proxy destination: <hash>"
    echo ""
    echo "Then update client.conf manually by setting:"
    echo "  [server]"
    echo "  proxy_destination = <hash>"
    exit 1
fi

echo "Found proxy destination: $PROXY_DEST"

# Update client.conf
if [ -f "client.conf" ]; then
    sed -i "s/^proxy_destination =.*/proxy_destination = $PROXY_DEST/" client.conf
    echo "Updated client.conf with proxy destination hash"
    echo ""
    echo "Verification:"
    grep "proxy_destination" client.conf
else
    echo "Error: client.conf not found in current directory"
    exit 1
fi

echo ""
echo "Done! You can now start the client with: python3 client.py"
