#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/futures-intraday}"

sudo mkdir -p "$APP_DIR"
sudo cp -r app.py static "$APP_DIR/"
sudo cp deploy/futures-intraday.service /etc/systemd/system/futures-intraday.service
sudo systemctl daemon-reload
sudo systemctl enable --now futures-intraday
sudo systemctl restart futures-intraday
sudo systemctl status futures-intraday --no-pager
