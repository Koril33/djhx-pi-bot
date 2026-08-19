#!/bin/bash
# djhx-pi-bot 部署脚本：由 Gitea Push webhook 触发（app.py /deploy-webhook）
# 流程：git pull → uv sync → systemctl restart
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

# 锁：防止并发部署
exec 9>/tmp/djhx-pi-bot-deploy.lock
flock -n 9 || { echo "[deploy] $(date -Is) 已有部署在运行，跳过"; exit 0; }

cd "$(dirname "$0")/.."
echo "[deploy] $(date -Is) 开始部署 (branch: $(git branch --show-current))"

echo "[deploy] git pull --ff-only"
git pull --ff-only

echo "[deploy] uv sync --frozen"
uv sync --frozen 2>&1 | tail -3 || uv sync 2>&1 | tail -3

echo "[deploy] systemctl restart djhx-pi-bot"
sudo /usr/bin/systemctl reset-failed djhx-pi-bot 2>/dev/null || true
sudo /usr/bin/systemctl restart djhx-pi-bot

echo "[deploy] $(date -Is) 部署完成"
