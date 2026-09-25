"""Imported at startup of the Whisper server's Python via vk_whisper_tuning.pth
(a .pth file, because the base image already has its own sitecustomize).

The bundled server calls faster-whisper without a beam size, so it always used
beam search with 5 candidates. WHISPER_BEAM_SIZE (in .env) sets it: 1 is greedy
decoding, which is much faster at a small accuracy cost on difficult audio.
"""
import os

beam = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))
if beam != 5:
    try:
        import faster_whisper.transcribe as fw

        original = fw.WhisperModel.transcribe

        def transcribe(self, audio, *args, **kwargs):
            kwargs.setdefault("beam_size", beam)
            kwargs.setdefault("best_of", beam)
            return original(self, audio, *args, **kwargs)

        fw.WhisperModel.transcribe = transcribe
    except ImportError:  # the gateway's interpreter shares this file but has no need for it
        pass
