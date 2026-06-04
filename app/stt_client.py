"""
語音轉文字（STT）模組
降級鏈：faster-whisper（GPU）→ faster-whisper（CPU）→ None
"""
import io
import os
import logging
import subprocess
import tempfile

logger = logging.getLogger(__name__)

WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "medium")
WHISPER_DEVICE    = os.getenv("WHISPER_DEVICE", "cuda")
WHISPER_LANGUAGE  = "zh"
WHISPER_CACHE_DIR = os.getenv("WHISPER_CACHE_DIR", "/opt/models/whisper")

_whisper_model = None


def _get_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel
        logger.info("載入 Whisper 模型 %s（%s）...", WHISPER_MODEL, WHISPER_DEVICE)
        _whisper_model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type="float16" if WHISPER_DEVICE == "cuda" else "int8",
            download_root=WHISPER_CACHE_DIR,
        )
        logger.info("Whisper 模型載入完成")
        return _whisper_model
    except Exception as e:
        logger.error("Whisper 模型載入失敗（%s），嘗試 CPU 模式：%s", WHISPER_DEVICE, e)
        try:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(
                WHISPER_MODEL, device="cpu", compute_type="int8",
                download_root=WHISPER_CACHE_DIR,
            )
            logger.info("Whisper CPU 模式載入完成")
            return _whisper_model
        except Exception as e2:
            logger.error("Whisper CPU 模式也失敗：%s", e2)
            return None


def _m4a_to_wav(audio_bytes: bytes) -> bytes | None:
    """M4A（LINE 語音格式）→ WAV（Whisper 輸入格式）"""
    try:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as src:
            src.write(audio_bytes)
            src_path = src.name

        dst_path = src_path.replace(".m4a", ".wav")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path,
             "-ar", "16000", "-ac", "1", "-f", "wav", dst_path],
            capture_output=True, timeout=30
        )
        os.unlink(src_path)

        if result.returncode != 0:
            logger.error("ffmpeg 轉換失敗：%s", result.stderr.decode()[:200])
            return None

        with open(dst_path, "rb") as f:
            wav_bytes = f.read()
        os.unlink(dst_path)
        return wav_bytes
    except Exception as e:
        logger.error("音訊轉換失敗：%s", e)
        return None


def transcribe(audio_bytes: bytes) -> str | None:
    """
    語音轉文字主入口。
    回傳辨識文字，失敗回傳 None。
    """
    wav_bytes = _m4a_to_wav(audio_bytes)
    if not wav_bytes:
        return None

    model = _get_model()
    if not model:
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            wav_path = f.name

        segments, info = model.transcribe(
            wav_path,
            language=WHISPER_LANGUAGE,
            beam_size=5,
            vad_filter=True,          # 靜音過濾
            vad_parameters={"min_silence_duration_ms": 500},
        )
        os.unlink(wav_path)

        text = " ".join(seg.text.strip() for seg in segments).strip()
        logger.info("STT 辨識完成（%.1fs，lang=%s）：%s",
                    info.duration, info.language, text[:60])
        return text or None
    except Exception as e:
        logger.error("Whisper 辨識失敗：%s", e)
        return None
