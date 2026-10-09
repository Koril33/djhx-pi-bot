"""隔离 Git fixture 和命令 shim；不会调用真实 sudo/systemctl。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
git_binary = shutil.which("git")
bash_binary = (str(Path(git_binary).parent.parent / "bin/bash.exe")
               if os.name == "nt" and git_binary else shutil.which("bash"))


@unittest.skipUnless(bash_binary and Path(bash_binary).is_file(), "需要 bash 验证部署脚本")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dj-deploy-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "deploy").mkdir()
        shutil.copyfile(ROOT / "deploy/deploy.sh", self.repo / "deploy/deploy.sh")
        shutil.copyfile(ROOT / "deploy/service_entry.py", self.repo / "deploy/service_entry.py")
        (self.repo / ".gitignore").write_text("data/\nlogs/\n.venv/\n")
        self.home = self.root / "home"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.log = self.root / "sudo.log"
        posix_home = self.home.as_posix()
        if os.name == "nt":
            posix_home = "/" + posix_home[0].lower() + posix_home[2:]
        self.environment = {**os.environ, "HOME": posix_home, "DJ_DEPLOY_TEST_LOG": self.log.as_posix()}
        self.write_script(self.bin / "sudo", 'printf "%s\\n" "$*" >> "$DJ_DEPLOY_TEST_LOG"\nexit 0\n')
        self.write_script(self.bin / "flock", "exit 0\n")
        self.write_script(self.bin / "uv", 'case "$*" in "sync --frozen") exit 1;; *) exit 0;; esac\n')
        python = self.repo / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        self.write_script(python,
                          'if [ "${1:-}" = "-" ] && [ "${2:-}" = "180" ]; then cat >/dev/null; exit 0; fi\n'
                          f'exec "{Path(sys.executable).as_posix()}" "$@"\n')
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", ".")
        self.git("commit", "-m", "initial fixture")
        self.previous = self.git("rev-parse", "HEAD").strip()
        self.origin = self.root / "origin.git"
        subprocess.run([git_binary, "init", "--bare", str(self.origin)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "origin", "main")

    @staticmethod
    def write_script(path, text):
        path.write_text("#!/bin/bash\n" + text, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run([git_binary, *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout

    def deploy(self, *args):
        return subprocess.run([bash_binary, "deploy/deploy.sh", *args], cwd=self.repo,
                              env=self.environment, capture_output=True, text=True, timeout=20)

    def test_dispatch_runs_in_independent_systemd_unit(self):
        response = self.deploy()
        self.assertEqual(response.returncode, 0, response.stderr)
        logged = self.log.read_text()
        self.assertIn("systemd-run", logged)
        self.assertIn("--collect", logged)
        self.assertIn("--apply", logged)

    def test_dirty_worktree_is_not_overwritten(self):
        draft = self.repo / "uncommitted.txt"
        draft.write_text("preserve me")
        response = self.deploy("--apply")
        self.assertNotEqual(response.returncode, 0)
        self.assertEqual(draft.read_text(), "preserve me")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)
        self.assertFalse(self.log.exists())

    def test_dependency_failure_restores_verified_clean_previous_commit(self):
        (self.repo / "change.txt").write_text("new revision")
        self.git("add", "change.txt")
        self.git("commit", "-m", "new target fixture")
        target = self.git("rev-parse", "HEAD").strip()
        self.git("push", "origin", "main")
        # 此临时 fixture 只含本测试创建的文件；恢复初始状态模拟线上落后。
        self.git("reset", "--hard", self.previous)
        # 模拟已安装 unit，避免依赖测试主机的 /etc 是否存在该服务。
        previous_unit = self.repo / "data/deploy-state/previous.service"
        previous_unit.parent.mkdir(parents=True)
        previous_unit.write_text("[Service]\nExecStart=fixture-old-entry\n")
        response = self.deploy("--apply")
        self.assertNotEqual(response.returncode, 0, response.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)
        result = json.loads((self.repo / "data/deploy-state/last-deploy.json").read_text())
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["target_commit"], target)
        self.assertEqual((self.repo / "data/deploy-state/failed-target").read_text().strip(), target)
        logged = self.log.read_text()
        self.assertIn("install -m 644", logged)
        self.assertIn("systemctl daemon-reload", logged)
        for action in ("stop", "start", "restart"):
            self.assertNotIn(f"systemctl {action} djhx-pi-bot", logged)

    def test_backup_failure_after_stop_restores_revision_and_restarts_service(self):
        self.environment["DJ_DEPLOY_TEST_UV_COUNT"] = (self.root / "uv-count").as_posix()
        self.write_script(self.bin / "uv", '''
if [ "$1" = "sync" ]; then exit 0; fi
count=0
if [ -f "$DJ_DEPLOY_TEST_UV_COUNT" ]; then count=$(cat "$DJ_DEPLOY_TEST_UV_COUNT"); fi
count=$((count + 1))
printf '%s' "$count" > "$DJ_DEPLOY_TEST_UV_COUNT"
cat >/dev/null
if [ "$count" -eq 3 ]; then exit 1; fi
exit 0
''')
        (self.repo / "change.txt").write_text("new revision")
        self.git("add", "change.txt")
        self.git("commit", "-m", "backup failure target fixture")
        target = self.git("rev-parse", "HEAD").strip()
        self.git("push", "origin", "main")
        self.git("reset", "--hard", self.previous)
        response = self.deploy("--apply")
        self.assertNotEqual(response.returncode, 0, response.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)
        logged = self.log.read_text()
        self.assertIn("systemctl stop djhx-pi-bot", logged)
        self.assertIn("systemctl restart djhx-pi-bot", logged)
        self.assertNotIn("systemctl start djhx-pi-bot", logged)
        self.assertTrue((self.repo / "data/deploy-state/service-entry.py").is_file())
        result = json.loads((self.repo / "data/deploy-state/last-deploy.json").read_text())
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["target_commit"], target)
