#!/bin/bash
# 唯一部署入口；独立 transient unit 保证 bot 重启不会杀掉部署/验证进程。
set -euo pipefail
umask 077
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd "$(dirname "$0")/.."
repo_root=$(pwd -P)
apply=false
catch_up=false
for argument in "$@"; do
  case "$argument" in
    --apply) apply=true ;;
    --catch-up) catch_up=true ;;
    *) echo "[deploy] 不支持的参数: $argument"; exit 2 ;;
  esac
done
mkdir -p logs data/deploy-state
chmod 700 data/deploy-state
if [ "$apply" = false ]; then
  args=(/bin/bash "$repo_root/deploy/deploy.sh" --apply)
  if [ "$catch_up" = true ]; then args+=(--catch-up); fi
  unit="djhx-pi-bot-deploy-$(date +%s%N)"
  sudo -n /usr/bin/systemd-run --collect --unit="$unit" \
    --property=User=pi --property="WorkingDirectory=$repo_root" \
    --property="Environment=HOME=/home/pi PATH=/home/pi/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    --property="StandardOutput=append:$repo_root/logs/deploy.log" \
    --property="StandardError=append:$repo_root/logs/deploy.log" "${args[@]}"
  echo "[deploy] 已启动独立部署任务 $unit"
  exit 0
fi
exec 9>/tmp/djhx-pi-bot-deploy.lock
flock 9
state_dir="$repo_root/data/deploy-state"
current_commit=""
target_commit=""
changed=false
stopped=false
write_result() {
  "$repo_root/.venv/bin/python" - "$state_dir/last-deploy.json" "$1" "$current_commit" "$target_commit" "$2" <<'PY'
import json, os, sys, time
path, state, previous, target, detail = sys.argv[1:]
temp = path + '.tmp'
with open(temp, 'w', encoding='utf-8') as stream:
    json.dump({'timestamp': time.time(), 'state': state, 'previous_commit': previous,
               'target_commit': target, 'detail': detail}, stream, ensure_ascii=False)
os.chmod(temp, 0o600)
os.replace(temp, path)
if state == 'success':
    success = os.path.join(os.path.dirname(path), 'last-success.json')
    import shutil
    shutil.copyfile(path, success)
    os.chmod(success, 0o600)
PY
}
check_health() {
  "$repo_root/.venv/bin/python" - "$1" "$2" "${3:-false}" <<'PY'
import json, sys, time, urllib.request
deadline = time.monotonic() + float(sys.argv[1])
expected_revision, allow_unknown = sys.argv[2], sys.argv[3] == 'true'
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8128/healthz', timeout=5) as response:
            health = json.load(response)
        revision_matches = health.get('revision') == expected_revision or (allow_unknown and not health.get('revision'))
        if health.get('ok') and not health.get('degraded') and revision_matches:
            print('[deploy] 服务就绪，运行提交:', health.get('revision'), '历史事件:', health.get('incidents', {}))
            break
    except (OSError, ValueError):
        pass
    time.sleep(3)
else:
    raise SystemExit('[deploy] 重启后就绪验证失败')
PY
}
on_exit() {
  rc=$?
  trap - EXIT
  if [ "$rc" -eq 0 ]; then return; fi
  echo "[deploy] $(date -Is) 部署失败，退出码 $rc"
  if [ -n "$target_commit" ]; then printf '%s\n' "$target_commit" > "$state_dir/failed-target"; fi
  if [ "$changed" = true ]; then
    if [ -z "$(git status --porcelain)" ] && [ "$(git rev-parse HEAD)" = "$target_commit" ]; then
      echo "[deploy] 恢复已验证洁净的旧提交 $current_commit"
      git reset --keep "$current_commit" || rc=1
      uv sync --frozen --offline || uv sync --frozen || rc=1
      if [ -f "$state_dir/previous.service" ]; then
        # 包装入口位于 ignored data，旧代码恢复后依然能阻止失败目标补偿循环。
        if [ -f "$state_dir/service-entry.py" ]; then
          sed "s|^ExecStart=.*|ExecStart=$repo_root/.venv/bin/python $state_dir/service-entry.py|" \
            "$state_dir/previous.service" > "$state_dir/recovered.service"
        else
          cp "$state_dir/previous.service" "$state_dir/recovered.service"
        fi
        sudo -n install -m 644 "$state_dir/recovered.service" /etc/systemd/system/djhx-pi-bot.service || rc=1
        sudo -n /usr/bin/systemctl daemon-reload || rc=1
      fi
    else
      echo "[deploy] 回滚中止：部署期间出现新的工作区变动，请人工核对"
    fi
  fi
  if [ "$stopped" = true ]; then
    sudo -n /usr/bin/systemctl restart djhx-pi-bot || rc=1
    check_health 180 "$current_commit" true || echo "[deploy] 旧版本恢复后的就绪验证也失败，请核对网络和日志"
  fi
  write_result failed "deployment failed; previous service restored when safe" || true
  exit "$rc"
}
trap on_exit EXIT

echo "[deploy] $(date -Is) 开始部署"
if [ "$(git branch --show-current)" != main ]; then echo "[deploy] 中止：当前分支不是 main"; exit 1; fi
if [ -n "$(git status --porcelain)" ]; then
  echo "[deploy] 中止：工作区有未提交改动，请先 add/commit/push"
  git status --porcelain
  exit 1
fi
GIT_TERMINAL_PROMPT=0 git fetch origin main
if [ "$(git rev-list --count origin/main..HEAD)" -gt 0 ]; then
  echo "[deploy] 中止：本地有未推送提交，请先 git push origin main"
  exit 1
fi
current_commit=$(git rev-parse HEAD)
target_commit=$(git rev-parse origin/main)
if [ "$catch_up" = true ]; then
  if [ "$current_commit" = "$target_commit" ]; then echo "[deploy] 提交一致，无需补偿部署"; exit 0; fi
  if [ -f "$state_dir/failed-target" ] && [ "$(cat "$state_dir/failed-target")" = "$target_commit" ]; then
    echo "[deploy] 已失败目标等待人工重试，避免启动回滚循环"; exit 0
  fi
fi
running_commit=$("$repo_root/.venv/bin/python" - "$state_dir/last-success.json" "$current_commit" <<'PY'
import json, sys, urllib.request
commit = None
try:
    with urllib.request.urlopen('http://127.0.0.1:8128/healthz', timeout=3) as response:
        commit = json.load(response).get('revision')
except (OSError, ValueError):
    pass
if not commit:
    try:
        with open(sys.argv[1], encoding='utf-8') as stream:
            commit = json.load(stream).get('target_commit')
    except (OSError, ValueError):
        pass
print(commit or sys.argv[2])
PY
)
if git cat-file -e "$running_commit^{commit}" 2>/dev/null && git merge-base --is-ancestor "$running_commit" "$target_commit"; then
  current_commit="$running_commit"
fi
write_result running "validation and backup in progress"
if [ -f /etc/systemd/system/djhx-pi-bot.service ]; then
  cp /etc/systemd/system/djhx-pi-bot.service "$state_dir/previous.service"
fi
git merge --ff-only origin/main
changed=true
cp deploy/service_entry.py "$state_dir/service-entry.py"
uv sync --frozen
uv run --frozen python - <<'PY'
import py_compile
from pathlib import Path
for path in [*Path('.').glob('*.py'), *Path('scripts').glob('*.py'), *Path('deploy').glob('*.py')]:
    py_compile.compile(str(path), doraise=True)
print('[deploy] Python 编译验证完成')
PY
if [ -f scripts/prepare_voice.py ]; then
  uv run --frozen python - <<'PY' || echo "[deploy] 语音模型未准备，可稍后重试"
from pathlib import Path
from config import Settings
from scripts.prepare_voice import prepare
settings = Settings()
if settings.voice_enabled:
    print(prepare(Path(settings.voice_model_dir)))
PY
fi
# 不打断正在执行的工具；忙碌超过两分钟则终止本次部署、恢复磁盘版本。
uv run --frozen python - <<'PY'
import json, time, urllib.request
from urllib.error import HTTPError
deadline = time.monotonic() + 120
while True:
    try:
        try:
            response = urllib.request.urlopen('http://127.0.0.1:8128/healthz', timeout=3)
        except HTTPError as error:
            response = error  # 即使 readiness 暂时异常，也不能忽略正在执行的任务。
        with response:
            health = json.load(response)
        activity = health.get('activity', {})
        busy = health.get('updates', {}).get('processing', 0) or activity.get('active_sessions', 0) or activity.get('summary_tasks', 0)
        if not busy:
            break
    except OSError:
        break
    if time.monotonic() >= deadline:
        raise SystemExit('[deploy] 服务仍在处理任务，本次部署延后')
    time.sleep(3)
PY
sudo -n /usr/bin/systemctl stop djhx-pi-bot
stopped=true
uv run --frozen python - <<'PY'
from pathlib import Path
from config import Settings
from ops import create_backup
settings = Settings()
if Path(settings.db_path).is_file():
    result = create_backup(settings.db_path, settings.pi_session_dir, settings.backup_root, reason='deploy')
    print('[deploy] 完整备份已生成:', result['archive'])
PY
sudo -n install -m 644 deploy/djhx-pi-bot.service /etc/systemd/system/djhx-pi-bot.service
sudo -n /usr/bin/systemctl daemon-reload
sudo -n /usr/bin/systemctl start djhx-pi-bot
check_health 600 "$target_commit"
rm -f "$state_dir/failed-target"
write_result success "compile, consistent backup and readiness checks passed"
stopped=false
echo "[deploy] $(date -Is) 部署完成 $target_commit"
