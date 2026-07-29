#!/usr/bin/env bash
# bootstrap-test-host.sh
# Prepares a test host for PatchPilot integration tests.
# Run this on the host (or inside the Docker container) after first boot.
set -euo pipefail

SSH_KEY_DIR="${HOME}/.ssh"
SSH_KEY="${SSH_KEY_DIR}/patchpilot_test_ed25519"

echo "=== PatchPilot Test Host Bootstrap ==="

# Generate SSH key if it doesn't exist
if [ ! -f "${SSH_KEY}" ]; then
    mkdir -p "${SSH_KEY_DIR}"
    ssh-keygen -t ed25519 -f "${SSH_KEY}" -N "" -C "patchpilot-test"
    echo "[OK] SSH key generated: ${SSH_KEY}"
else
    echo "[OK] SSH key already exists: ${SSH_KEY}"
fi

# Ensure public key is in authorized_keys for the deploy user
AUTH_KEYS="/home/deploy/.ssh/authorized_keys"
if [ -f "${SSH_KEY}.pub" ]; then
    sudo mkdir -p /home/deploy/.ssh
    cat "${SSH_KEY}.pub" | sudo tee -a "${AUTH_KEYS}" > /dev/null
    sudo chown -R deploy:deploy /home/deploy/.ssh
    sudo chmod 600 "${AUTH_KEYS}"
    sudo chmod 700 /home/deploy/.ssh
    echo "[OK] Public key added to ${AUTH_KEYS}"
fi

# Ensure required packages are installed
if command -v apt-get &> /dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq nginx curl netcat-openbsd python3 > /dev/null 2>&1
elif command -v dnf &> /dev/null; then
    sudo dnf install -y nginx curl nmap-ncat python3 > /dev/null 2>&1
fi

# Start services
sudo systemctl enable ssh
sudo systemctl start ssh
sudo systemctl enable nginx
sudo systemctl start nginx

echo "[OK] SSH and nginx services running"

# Create a simple health endpoint
echo '{"status":"ok"}' | sudo tee /var/www/html/health.json > /dev/null

echo "=== Bootstrap complete ==="
echo "SSH key: ${SSH_KEY}"
echo "Test host IP: $(hostname -I | awk '{print $1}')"
