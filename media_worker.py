"""隔离附件解析：无网络、无 Pi 工具，退出即释放语音模型内存。"""

import json
import os
import sys
from pathlib import Path


def read_pdf(path: str, *, max_pages: int, max_chars: int) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        raise ValueError("暂不读取加密 PDF，请解密后重发")
    pages = len(reader.pages)
    if not pages or pages > max_pages:
        raise ValueError(f"PDF 页数为 {pages}，最多支持 {max_pages} 页，请拆分后重发")
    parts = []
    size = 0
    truncated = False
    read_pages = 0
    for index, page in enumerate(reader.pages, 1):
        content = page.get_contents()
        if content is not None and len(content.get_data()) > 8 * 1024 * 1024:
            raise ValueError("PDF 单页内容过大，请压缩或拆分后重发")
        text = (page.extract_text() or "").strip()
        read_pages = index
        if not text:
            continue
        block = f"[第 {index} 页]\n{text}\n"
        remaining = max_chars - size
        if len(block) > remaining:
            parts.append(block[:remaining])
            truncated = True
            break
        parts.append(block)
        size += len(block)
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError("PDF 没有可提取的文字，可能是扫描件；请发送图片或含文字的 PDF")
    if truncated:
        text += f"\n[提取已截断：最多 {max_chars} 字符，仅读取到第 {read_pages}/{pages} 页]"
    return {"ok": True, "text": text, "pages": pages, "read_pages": read_pages,
            "truncated": truncated}


def read_voice(path: str, *, model_dir: str, max_seconds: float) -> dict:
    import av
    import numpy as np

    chunks = []
    samples = 0
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(path) as container:
        audio = next((stream for stream in container.streams if stream.type == "audio"), None)
        if audio is None:
            raise ValueError("文件不含可识别的音轨")
        for frame in container.decode(audio):
            for decoded in resampler.resample(frame):
                values = decoded.to_ndarray().flatten()
                samples += len(values)
                if samples > max_seconds * 16000:
                    raise ValueError(f"语音超过 {max_seconds:g} 秒，请拆分后重发")
                chunks.append(values)
        for decoded in resampler.resample(None):
            values = decoded.to_ndarray().flatten()
            samples += len(values)
            if samples > max_seconds * 16000:
                raise ValueError(f"语音超过 {max_seconds:g} 秒，请拆分后重发")
            chunks.append(values)
    if not chunks:
        raise ValueError("语音为空，请重新录制")
    waveform = np.concatenate(chunks).astype(np.float32) / 32768.0
    if float(np.abs(waveform).max()) < .003:
        raise ValueError("语音音量过低或只有静音，请重新录制")
    from faster_whisper import WhisperModel

    model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=1,
                         num_workers=1, local_files_only=True)
    segments, info = model.transcribe(
        waveform, beam_size=1, condition_on_previous_text=False, vad_filter=False,
        no_speech_threshold=.7, compression_ratio_threshold=2.4,
        initial_prompt="中文或 English，Telegram 语音消息。",
    )
    text = "".join(segment.text for segment in segments).strip()
    if not text:
        raise ValueError("没有识别出语音内容，请重新录制或发送文字")
    if len(text) > 12000:
        raise ValueError("转写内容过长，请拆分后重发")
    return {"ok": True, "text": text, "language": info.language,
            "seconds": round(samples / 16000, 2)}


def main() -> None:
    kind, path, options_json = sys.argv[1:]
    options = json.loads(options_json)
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (110 if kind == "voice" else 18, 115 if kind == "voice" else 20))
        if kind == "pdf":
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    try:
        result = read_pdf(path, **options) if kind == "pdf" else read_voice(path, **options)
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}
    except MemoryError:
        result = {"ok": False, "error": "附件需要的内存过多，请拆分后重发"}
    except Exception:
        result = {"ok": False, "error": "无法读取附件，请检查文件格式或重新录制"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
