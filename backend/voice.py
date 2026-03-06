"""Voice services: speech-to-text (STT) and text-to-speech (TTS)."""
from __future__ import annotations

import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

# ─── STT dependency (faster-whisper) ─────────────────────────────────────────
try:
    from faster_whisper import WhisperModel  # type: ignore

    _WHISPER_OK = True
except ImportError:
    _WHISPER_OK = False
    logger.warning(
        "faster-whisper not installed — STT unavailable. "
        "Run: pip install faster-whisper"
    )

# ─── TTS dependency (pyttsx3) ────────────────────────────────────────────────
try:
    import pyttsx3  # type: ignore

    _PYTTSX3_OK = True
except ImportError:
    _PYTTSX3_OK = False
    logger.warning(
        "pyttsx3 not installed — TTS unavailable. "
        "Run: pip install pyttsx3"
    )


class VoiceService:
    """Handles speech-to-text transcription and text-to-speech playback.

    STT uses faster-whisper (tiny model, CPU, int8) — no GPU required.
    TTS uses pyttsx3 with the Windows SAPI5 engine (Microsoft Zira preferred).
    Both are optional; availability is reported via :attr:`stt_available` and
    :attr:`tts_available`.
    """

    def __init__(self, whisper_model_size: str = "tiny") -> None:
        self._model_size = whisper_model_size
        self._whisper: object | None = None
        self._tts_lock = threading.Lock()
        if _WHISPER_OK:
            self._load_whisper()

    # ─── Whisper STT ─────────────────────────────────────────────────────────

    def _load_whisper(self) -> None:
        try:
            self._whisper = WhisperModel(  # type: ignore[name-defined]
                self._model_size, device="cpu", compute_type="int8"
            )
            logger.info("Whisper STT ready (model=%s, device=cpu)", self._model_size)
        except Exception:
            logger.exception("Failed to initialise Whisper model")
            self._whisper = None

    def transcribe(self, audio_bytes: bytes, audio_suffix: str = ".webm") -> str:
        """Transcribe raw audio bytes to text.

        Writes a temporary file so faster-whisper's libav decoder can handle
        any container format (webm, ogg, wav, mp4 …).

        Raises
        ------
        RuntimeError
            When faster-whisper is not installed or the model failed to load.
        """
        if not _WHISPER_OK or self._whisper is None:
            raise RuntimeError(
                "Whisper STT is not available. Install faster-whisper."
            )
        with tempfile.NamedTemporaryFile(
            suffix=audio_suffix, delete=False
        ) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            segments, _ = self._whisper.transcribe(  # type: ignore[union-attr]
                tmp_path, language="en", beam_size=1, vad_filter=True
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            logger.info("STT transcribed %d chars", len(text))
            return text
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ─── pyttsx3 TTS ─────────────────────────────────────────────────────────

    def speak(self, text: str) -> None:
        """Speak *text* in a daemon thread so the API call returns immediately."""
        if not _PYTTSX3_OK:
            return
        t = threading.Thread(
            target=self._speak_worker, args=(text,), daemon=True, name="tts"
        )
        t.start()

    def _speak_worker(self, text: str) -> None:
        # pyttsx3 must be initialised in the thread that calls runAndWait on
        # Windows (SAPI5), so we init + destroy per call under a lock.
        with self._tts_lock:
            try:
                engine = pyttsx3.init()  # type: ignore[name-defined]
                # Prefer Microsoft Zira (female) for a friendlier tone.
                for voice in engine.getProperty("voices") or []:
                    if "zira" in voice.id.lower():
                        engine.setProperty("voice", voice.id)
                        break
                engine.setProperty("rate", 170)
                engine.setProperty("volume", 0.9)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
            except Exception:
                logger.exception("TTS playback error")

    # ─── Availability flags ───────────────────────────────────────────────────

    @property
    def stt_available(self) -> bool:
        """True when faster-whisper is installed and the model loaded."""
        return _WHISPER_OK and self._whisper is not None

    @property
    def tts_available(self) -> bool:
        """True when pyttsx3 is installed."""
        return _PYTTSX3_OK
