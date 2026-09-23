#!/bin/bash
# djhx-pi-bot 部署脚本：由 Gitea Push webhook 触发（app.py /deploy-webhook）
# 流程：fetch → 验证 → fast-forward → 冻结依赖同步 → systemctl restart
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

if [ "${1:-}" != "" ] && [ "${1:-}" != "--catch-up" ]; then
  echo "[deploy] 不支持的参数: $1"
  exit 2
fi

# 锁：同一服务周期内串行执行；重启杀掉的排队进程由启动补偿检查兜底。
exec 9>/tmp/djhx-pi-bot-deploy.lock
flock 9

cd "$(dirname "$0")/.."
echo "[deploy] $(date -Is) 开始部署 (branch: $(git branch --show-current))"
trap 'rc=$?; if [ "$rc" -ne 0 ]; then echo "[deploy] $(date -Is) 部署失败，退出码: $rc"; fi' EXIT

if [ "$(git branch --show-current)" != "main" ]; then
  echo "[deploy] 中止：当前分支不是 main"
  exit 1
fi

echo "[deploy] git fetch origin main"
GIT_TERMINAL_PROMPT=0 git fetch origin main

# 护栏：工作区不干净或有未推送提交时中止（常见于服务器 pi 直接改代码未推送）
if [ -n "$(git status --porcelain)" ]; then
  echo "[deploy] 中止：工作区有未提交改动，部署会覆盖它们："
  git status --porcelain | head -10
  echo "[deploy] 提示：先在服务器上 git add/commit/push，再触发部署"
  exit 1
fi
if [ "$(git rev-list --count origin/main..HEAD)" -gt 0 ]; then
  echo "[deploy] 中止：本地有未推送的提交，pull 将无法 fast-forward："
  git log --oneline origin/main..HEAD | head -5
  echo "[deploy] 提示：先 git push origin main 推送这些提交"
  exit 1
fi

current_commit=$(git rev-parse HEAD)
target_commit=$(git rev-parse origin/main)
echo "[deploy] 当前提交: $current_commit, 目标提交: $target_commit"
if [ "$current_commit" = "$target_commit" ] && [ "${1:-}" = "--catch-up" ]; then
  echo "[deploy] $(date -Is) 已是最新提交，无需重启"
  exit 0
fi

# webhook 可能由服务器本机 push 触发：代码已经是 HEAD，但运行中的服务仍是旧进程。
# 因此普通 webhook 即使提交相同也要同步依赖并重启；仅启动补偿可以无操作退出。

echo "[deploy] git merge --ff-only origin/main"
git merge --ff-only origin/main

echo "[deploy] uv sync --frozen"
uv sync --frozen

echo "[deploy] systemctl restart djhx-pi-bot"
echo "[deploy] $(date -Is) 代码与依赖已就绪: $(git rev-parse HEAD)，开始重启"
sudo -n /usr/bin/systemctl restart djhx-pi-bot

echo "[deploy] $(date -Is) 部署完成"
