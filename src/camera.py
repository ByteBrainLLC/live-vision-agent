import io

import cv2
from PIL import Image

from src.config import MAX_FRAME_SIZE, JPEG_QUALITY


class Camera:
    def __init__(self, device_index: int = 0) -> None:
        self._capture = cv2.VideoCapture(device_index)
        if not self._capture.isOpened():
            raise RuntimeError(
                f"Could not open camera at index {device_index}. "
                "Close other apps using the webcam (Zoom, Teams, browser tabs, OBS) "
                "and try again, or pass Camera(device_index=1) for an external cam."
            )

    def read_jpeg_frame(self) -> bytes | None:
        ok, frame_bgr = self._capture.read()
        if not ok or frame_bgr is None:
            return None
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame_rgb)
        image.thumbnail(MAX_FRAME_SIZE)  # preserves aspect ratio
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
