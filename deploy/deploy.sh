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

# 护栏：工作区不干净或有未推送提交时中止（常见于服务器 pi 直接改代码未推送）
if [ -n "$(git status --porcelain)" ]; then
  echo "[deploy] 中止：工作区有未提交改动，部署会覆盖它们："
  git status --porcelain | head -10
  echo "[deploy] 提示：先在服务器上 git add/commit/push，再触发部署"
  exit 1
fi
if [ "$(git rev-list --count HEAD..@{u} 2>/dev/null)" -gt 0 ]; then
  echo "[deploy] 中止：本地有未推送的提交，pull 将无法 fast-forward："
  git log --oneline @{u}..HEAD | head -5
  echo "[deploy] 提示：先 git push origin main 推送这些提交"
  exit 1
fi

echo "[deploy] git pull --ff-only"
git pull --ff-only

echo "[deploy] uv sync --frozen"
uv sync --frozen 2>&1 | tail -3 || uv sync 2>&1 | tail -3

echo "[deploy] systemctl restart djhx-pi-bot"
sudo /usr/bin/systemctl reset-failed djhx-pi-bot 2>/dev/null || true
sudo /usr/bin/systemctl restart djhx-pi-bot

echo "[deploy] $(date -Is) 部署完成"
