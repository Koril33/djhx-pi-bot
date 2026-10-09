"""下载固定官方 tiny 模型；日常转写不会连接网络。"""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

from config import Settings


MODEL = "Systran/faster-whisper-tiny"
REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"
FILES = {
    "config.json": (2249, "3baa18e2b321a2f489614607852a729fcd516480", "git"),
    "model.bin": (75538270, "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1", "sha256"),
    "tokenizer.json": (2203239, "7818adb6de9fa3064d3ff81226fdd675be1f6344", "git"),
    "vocabulary.txt": (459861, "c9074644d9d1205686f16d411564729461324b75", "git"),
}


def verify(path: Path, size: int, digest: str, kind: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    content = path.read_bytes()
    actual = (hashlib.sha256(content).hexdigest() if kind == "sha256" else
              hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest())
    return actual == digest


def prepare(destination: Path) -> dict:
    destination = destination.resolve()
    if all(verify(destination / name, *meta) for name, meta in FILES.items()):
        return {"ok": True, "model": MODEL, "revision": REVISION, "changed": False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".voice-model-", dir=destination.parent) as folder:
        stage = Path(folder)
        for name, meta in FILES.items():
            target = stage / name
            for attempt in range(3):
                try:
                    req = urllib.request.Request(f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}",
                                                 headers={"User-Agent": "djhx-pi-bot-voice/1.0"})
                    with urllib.request.urlopen(req, timeout=120) as response, target.open("wb") as output:
                        shutil.copyfileobj(response, output)
                    if not verify(target, *meta):
                        raise RuntimeError("语音模型文件校验失败")
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
        destination.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            destination.chmod(0o700)
        for name in FILES:
            os.replace(stage / name, destination / name)
            if os.name == "posix":
                (destination / name).chmod(0o600)
        manifest = {"model": MODEL, "revision": REVISION, "files": FILES}
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {"ok": True, "model": MODEL, "revision": REVISION, "changed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    path = args.destination or Path(Settings().voice_model_dir)
    print(json.dumps(prepare(path)))
