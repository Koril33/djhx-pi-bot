"""受限 PDF 阅读与本地语音转写，解析进程可随 /stop 取消。"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


class MediaError(ValueError):
    pass


async def _worker(kind: str, data: bytes, settings) -> dict:
    maximum = settings.pdf_max_bytes if kind == "pdf" else settings.voice_max_bytes
    if not data or len(data) > maximum:
        raise MediaError(f"附件为空或超过 {maximum // 1024 // 1024} MiB 限制")
    timeout = (settings.pdf_parse_timeout_seconds if kind == "pdf"
               else settings.voice_transcribe_timeout_seconds)
    options = ({"max_pages": settings.pdf_max_pages, "max_chars": settings.pdf_max_chars}
               if kind == "pdf" else {
                   "model_dir": str(Path(settings.voice_model_dir).resolve()),
                   "max_seconds": settings.voice_max_seconds,
               })
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "HF_HUB_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"}
    with tempfile.TemporaryDirectory(prefix="djhx-media-") as folder:
        if os.name == "posix":
            Path(folder).chmod(0o700)
        path = Path(folder) / ("input.pdf" if kind == "pdf" else "input.audio")
        path.write_bytes(data)
        if os.name == "posix":
            path.chmod(0o600)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("media_worker.py")), kind,
            str(path), json.dumps(options), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.CancelledError, TimeoutError):
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            if asyncio.current_task().cancelling():
                raise
            raise MediaError("附件处理超时，请缩短或拆分后重发") from None
    try:
        result = json.loads(stdout)
    except (UnicodeError, ValueError):
        raise MediaError("附件处理失败或资源不足，请缩小后重发") from None
    if proc.returncode or not result.get("ok"):
        raise MediaError(result.get("error") or "附件处理失败，请检查文件格式")
    return result


async def extract_pdf(data: bytes, settings) -> dict:
    if not data.startswith(b"%PDF-"):
        raise MediaError("附件内容不是有效 PDF")
    return await _worker("pdf", data, settings)


async def transcribe_voice(data: bytes, settings) -> dict:
    if not settings.voice_enabled:
        raise MediaError("语音转写尚未启用，请发送文字")
    folder = Path(settings.voice_model_dir)
    if not all((folder / name).is_file() for name in ("model.bin", "tokenizer.json", "config.json")):
        raise MediaError("本地语音模型尚未准备好，请先发送文字")
    return await _worker("voice", data, settings)
