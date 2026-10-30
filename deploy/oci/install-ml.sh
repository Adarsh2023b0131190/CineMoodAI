#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/opt/cinemood/repo}"
if [[ ! -f "$REPO_DIR/ml-service/requirements.txt" ]]; then
  echo "Expected CineMood checkout at $REPO_DIR" >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3.12-venv python3-pip
sudo useradd --system --home /opt/cinemood --shell /usr/sbin/nologin cinemood || true
sudo install -d -o cinemood -g cinemood /opt/cinemood /var/lib/cinemood /etc/cinemood
sudo python3.12 -m venv /opt/cinemood/venv
sudo /opt/cinemood/venv/bin/pip install --no-cache-dir -r "$REPO_DIR/ml-service/requirements.txt"
sudo chown -R cinemood:cinemood /opt/cinemood/venv
sudo install -m 0644 "$REPO_DIR/deploy/oci/cinemood-ml.service" /etc/systemd/system/cinemood-ml.service

if [[ ! -f /etc/cinemood/ml.env ]]; then
  sudo install -m 0600 /dev/null /etc/cinemood/ml.env
  echo "Created /etc/cinemood/ml.env. Add CINEMOOD_DATA_BUNDLE_URL, CINEMOOD_DATA_BUNDLE_SHA256, and ML_SERVICE_TOKEN there, then start cinemood-ml." >&2
else
  sudo chmod 0600 /etc/cinemood/ml.env
fi

sudo systemctl daemon-reload
sudo systemctl enable cinemood-ml
if sudo grep -qE '^CINEMOOD_DATA_BUNDLE_URL=.+$' /etc/cinemood/ml.env \
  && sudo grep -qE '^CINEMOOD_DATA_BUNDLE_SHA256=[a-fA-F0-9]{64}$' /etc/cinemood/ml.env \
  && sudo grep -qE '^ML_SERVICE_TOKEN=.{32,}$' /etc/cinemood/ml.env; then
  sudo systemctl restart cinemood-ml
else
  echo "CineMood ML service is installed but not started: required settings are missing from /etc/cinemood/ml.env." >&2
fi
