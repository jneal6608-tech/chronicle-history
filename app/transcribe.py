"""Speech-to-text with word timestamps using faster-whisper (free, local, CPU-friendly)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from . import config


class TranscriptionError(Exception):
    pass


_MODEL = None
_MODEL_NAME = None


def _device() -> tuple[str, str]:
    dev = config.WHISPER_DEVICE
    if dev == "auto":
        try:
            import ctranslate2
            dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            dev = "cpu"
    return dev, ("float16" if dev == "cuda" else "int8")


def load_model():
    global _MODEL, _MODEL_NAME
    name = config.WHISPER_MODEL
    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscriptionError("faster-whisper is not installed. Run the installer again "
                                 "(in the Linux Terminal: cd chronicle-shorts && bash install.sh).") from e
    device, compute = _device()
    model_ref = name
    if not Path(name).exists() and config.WHISPER_LANGUAGE == "en" and name in ("tiny", "base", "small", "medium"):
        model_ref = name + ".en"  # English-only models are more accurate for English narration
    try:
        _MODEL = WhisperModel(model_ref, device=device, compute_type=compute,
                              download_root=str(config.DATA_DIR / "models"))
    except Exception as e:  # network / disk problems on first download
        raise TranscriptionError(
            f"Could not load the speech-recognition model '{model_ref}'. The first run downloads it "
            f"from huggingface.co (about 75 MB for base, 480 MB for small) - check your internet "
            f"connection and free disk space. Details: {e}") from e
    _MODEL_NAME = name
    return _MODEL


def transcribe(wav: Path, duration: float, out_json: Path,
               progress: Optional[Callable[[float, str], None]] = None) -> dict:
    model = load_model()
    lang = None if config.WHISPER_LANGUAGE == "auto" else config.WHISPER_LANGUAGE
    try:
        import wave

        import numpy as np
        with wave.open(str(wav), "rb") as wf:  # 16 kHz mono PCM written by FFmpeg
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = model.transcribe(
            audio, language=lang, word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500}, beam_size=5,
            condition_on_previous_text=False)
        words, segs = [], []
        for seg in segments:
            seg_words = []
            for w in seg.words or []:
                text = w.word.strip()
                if not text:
                    continue
                item = {"w": text, "s": round(float(w.start), 3), "e": round(float(w.end), 3),
                        "p": round(float(w.probability), 3)}
                words.append(item)
                seg_words.append(item)
            segs.append({"s": round(seg.start, 3), "e": round(seg.end, 3), "text": seg.text.strip()})
            if progress:
                progress(min(1.0, seg.end / max(duration, 1)), seg.text.strip()[:80])
    except Exception as e:
        raise TranscriptionError(f"Transcription failed: {e}") from e
    if len(words) < 40:
        raise TranscriptionError("Almost no speech was detected in this video. Chronicle Shorts needs "
                                 "narration to choose moments and create captions.")
    # Guarantee monotonic, non-overlapping word times.
    for a, b in zip(words, words[1:]):
        if b["s"] < a["e"]:
            mid = (a["e"] + b["s"]) / 2
            a["e"] = b["s"] = round(mid, 3)
        if b["e"] < b["s"]:
            b["e"] = b["s"]
    data = {"language": info.language, "model": config.WHISPER_MODEL, "words": words, "segments": segs}
    out_json.write_text(json.dumps(data), encoding="utf-8")
    return data
