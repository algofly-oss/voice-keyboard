"""Imported at startup of the Whisper server's Python via vk_whisper_tuning.pth
(a .pth file, because the base image already has its own sitecustomize).

The bundled server calls faster-whisper with a few fixed options; this adjusts
them for every request:

- WHISPER_BEAM_SIZE: beam search width (default 5, the server's own default).
- English letters for English/Hinglish/translation are handled by the gateway
  (hinglish.py) after recognition: suppressing non-Latin tokens here instead
  cost ~1.6 s per request.
- Temperature fallback: the server always sends a single temperature, which
  disables faster-whisper's retry when a result is repetitive ("jājājā…");
  the standard fallback steps are restored.
"""
import os

BEAM = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))
FALLBACK = os.environ.get("WHISPER_TEMPERATURE_FALLBACK", "true").strip().lower() not in {"0", "false", "no", "off"}
FALLBACK_TEMPERATURES = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]


try:
    import faster_whisper.transcribe as fw

    original = fw.WhisperModel.transcribe

    def transcribe(self, audio, *args, **kwargs):
        if BEAM != 5:
            kwargs.setdefault("beam_size", BEAM)
            kwargs.setdefault("best_of", BEAM)
        temperature = kwargs.get("temperature", 0.0)
        if FALLBACK and isinstance(temperature, (int, float)) and temperature == 0:
            kwargs["temperature"] = FALLBACK_TEMPERATURES
        return original(self, audio, *args, **kwargs)

    fw.WhisperModel.transcribe = transcribe
except ImportError:  # not the Whisper server's interpreter
    pass
