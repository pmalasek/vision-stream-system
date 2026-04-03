import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

VIDEO_FPS = 25
VIDEO_CODEC = "mp4v"
VIDEO_FILENAME = "output.mp4"
METADATA_FILENAME = "detections.jsonl"


class VideoRecorder:
    """Writes processed frames to an MP4 file and detection metadata to a JSONL file.

    The output is stored under a timestamped sub-directory of *output_dir*:

        <output_dir>/YYYY-MM-DD_HH-MM-SS/output.mp4
        <output_dir>/YYYY-MM-DD_HH-MM-SS/detections.jsonl

    The VideoWriter is created lazily on the first call to :meth:`write_frame`
    so that the frame dimensions are known at that point.
    """

    def __init__(self, output_dir: str) -> None:
        """Prepare the session output directory.

        Args:
            output_dir: Root directory under which a timestamped sub-directory
                        will be created for this recording session.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = os.path.join(output_dir, timestamp)
        os.makedirs(self.session_dir, exist_ok=True)

        self.video_path = os.path.join(self.session_dir, VIDEO_FILENAME)
        self.metadata_path = os.path.join(self.session_dir, METADATA_FILENAME)

        # Initialised lazily in write_frame once frame dimensions are known.
        self._writer: Optional[cv2.VideoWriter] = None
        self._metadata_file = open(self.metadata_path, "a", encoding="utf-8")  # noqa: WPS515

        logger.info("VideoRecorder session started → dir='%s'", self.session_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_frame(
        self,
        frame: np.ndarray,
        frame_id: int,
        detections: list[dict],
        timestamp: float,
    ) -> None:
        """Persist a single frame and its detection metadata.

        Args:
            frame:      BGR image as a NumPy array (H × W × 3).
            frame_id:   Monotonically increasing frame counter.
            detections: List of detection dicts produced by
                        :class:`~detector.PersonDetector`.
            timestamp:  Unix timestamp (seconds since epoch) for this frame,
                        as returned by ``time.time()``.
        """
        if self._writer is None:
            self._init_writer(frame)

        if self._writer is not None:
            self._writer.write(frame)

        record = {
            "frame_id": frame_id,
            "timestamp": timestamp,
            "person_count": len(detections),
            "detections": detections,
        }
        self._metadata_file.write(json.dumps(record) + "\n")
        self._metadata_file.flush()

    def release(self) -> None:
        """Release the VideoWriter and close the metadata file."""
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            logger.info("VideoWriter released → '%s'", self.video_path)

        if self._metadata_file and not self._metadata_file.closed:
            self._metadata_file.close()
            logger.info("Metadata file closed → '%s'", self.metadata_path)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_output_dir(self) -> str:
        """Absolute path of the current session directory."""
        return self.session_dir

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_writer(self, frame: np.ndarray) -> None:
        """Create the cv2.VideoWriter using dimensions taken from *frame*."""
        height, width = frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*VIDEO_CODEC)
        self._writer = cv2.VideoWriter(
            self.video_path, fourcc, VIDEO_FPS, (width, height)
        )

        if not self._writer.isOpened():
            logger.error(
                "Failed to open VideoWriter for '%s' – frames will not be saved.",
                self.video_path,
            )
            self._writer = None
            return

        logger.info(
            "VideoWriter initialised → '%s' (%dx%d @ %d fps)",
            self.video_path,
            width,
            height,
            VIDEO_FPS,
        )

    def __enter__(self) -> "VideoRecorder":
        return self

    def __exit__(self, *_) -> None:
        self.release()
