import asyncio
import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config import Settings
from interactions import PendingActions
from media import MediaError, extract_pdf, transcribe_voice
from media_worker import read_voice


def pdf_bytes(text="Latest decision: release 1.1.0", *, encrypted=False):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret-for-test")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pdf_worker_extracts_text(self):
        result = await extract_pdf(pdf_bytes(), Settings(_env_file=None))
        self.assertIn("release 1.1.0", result["text"])
        self.assertFalse(result["truncated"])

    async def test_pdf_encrypted_invalid_and_size_limits(self):
        settings = Settings(_env_file=None)
        with self.assertRaisesRegex(MediaError, "加密"):
            await extract_pdf(pdf_bytes(encrypted=True), settings)
        with self.assertRaises(MediaError):
            await extract_pdf(b"not pdf", settings)
        settings.pdf_max_bytes = 10
        with self.assertRaises(MediaError):
            await extract_pdf(pdf_bytes(), settings)

    async def test_pdf_truncation_is_explicit(self):
        settings = Settings(_env_file=None, pdf_max_chars=28)
        result = await extract_pdf(pdf_bytes("A long decision must be reported as truncated when limits are exceeded"), settings)
        self.assertTrue(result["truncated"])
        self.assertIn("提取已截断", result["text"])

    async def test_voice_disabled_and_missing_model(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(_env_file=None, voice_model_dir=folder)
            with self.assertRaisesRegex(MediaError, "尚未准备"):
                await transcribe_voice(b"voice", settings)
            settings.voice_enabled = False
            with self.assertRaisesRegex(MediaError, "尚未启用"):
                await transcribe_voice(b"voice", settings)

    async def test_media_cancellation_terminates_subprocess(self):
        process = AsyncMock()
        process.returncode = None
        process.terminate = lambda: setattr(process, "returncode", -15)
        process.wait.return_value = -15
        waiting = asyncio.Event()
        async def communicate():
            waiting.set()
            await asyncio.Future()
        process.communicate.side_effect = communicate
        with patch("media.asyncio.create_subprocess_exec", return_value=process):
            task = asyncio.create_task(extract_pdf(pdf_bytes(), Settings(_env_file=None)))
            await waiting.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(process.returncode, -15)

    def test_voice_overlong_before_model_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "long.wav")
            with wave.open(path, "wb") as out:
                out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                out.writeframes(b"\0\0" * 16000 * 2)
            with self.assertRaisesRegex(ValueError, "超过"):
                read_voice(path, model_dir="missing-model", max_seconds=1)


class PendingActionTests(unittest.TestCase):
    def test_owner_expiry_restart_and_single_consumption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "bot.db")
            first = PendingActions(path)
            ident = first.create(7, 7, "voice", {"text": "only confirmed request"})
            first.close()
            second = PendingActions(path)
            self.assertIsNone(second.claim(8, ident))
            self.assertIsNone(second.claim(7, ident, "other-kind"))
            self.assertEqual(second.claim(7, ident)["payload"]["text"], "only confirmed request")
            self.assertIsNone(second.claim(7, ident))
            expired = second.create(7, 7, "voice", {"text": "expired"}, ttl=.001)
            with patch("interactions.time.time", return_value=10**12):
                self.assertIsNone(second.claim(7, expired))
                second.prune()
            second.close()
