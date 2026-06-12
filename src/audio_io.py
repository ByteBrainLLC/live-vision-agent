import pyaudio

from src.config import (
    AUDIO_FORMAT,
    CHANNELS,
    SEND_SAMPLE_RATE,
    RECEIVE_SAMPLE_RATE,
    CHUNK_SIZE,
)


class AudioIO:
    def __init__(self) -> None:
        self._pyaudio = pyaudio.PyAudio()
        self._mic_stream: pyaudio.Stream | None = None
        self._speaker_stream: pyaudio.Stream | None = None

    def open_mic(self) -> pyaudio.Stream:
        if self._mic_stream is None:
            device = self._pyaudio.get_default_input_device_info()
            self._mic_stream = self._pyaudio.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=SEND_SAMPLE_RATE,
                input=True,
                input_device_index=int(device["index"]),
                frames_per_buffer=CHUNK_SIZE,
            )
        return self._mic_stream

    def open_speaker(self) -> pyaudio.Stream:
        if self._speaker_stream is None:
            self._speaker_stream = self._pyaudio.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=RECEIVE_SAMPLE_RATE,
                output=True,
            )
        return self._speaker_stream

    def close(self) -> None:
        for stream in (self._mic_stream, self._speaker_stream):
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self._mic_stream = None
        self._speaker_stream = None
        try:
            self._pyaudio.terminate()
        except Exception:
            pass
