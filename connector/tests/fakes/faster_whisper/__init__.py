"""Test stand-in for faster-whisper (the real model is a 1.5 GB download).
Only used by script tests that put tests/fakes on PYTHONPATH."""


class WhisperModel:
    def __init__(self, name, device="cpu", compute_type="int8", **kw):
        self.name = name

    def transcribe(self, path, **kw):
        class Info:
            language = "ar"
        return iter(()), Info()
