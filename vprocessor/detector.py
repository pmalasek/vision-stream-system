import logging

import cv2
import numpy as np
import torch
from ultralytics import YOLO

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0
BOX_COLOR = (0, 255, 0)  # green
BOX_THICKNESS = 2
LABEL_BG_COLOR = (0, 0, 0)  # black
LABEL_TEXT_COLOR = (0, 255, 0)  # green
LABEL_FONT = cv2.FONT_HERSHEY_SIMPLEX
LABEL_FONT_SCALE = 0.55
LABEL_FONT_THICKNESS = 1
LABEL_PADDING = 4


class PersonDetector:
    """Wraps a YOLOv8 model and filters detections to persons only (class 0)."""

    def __init__(self, model_path: str, confidence: float) -> None:
        """Load the YOLOv8 model.

        Args:
            model_path: Path to a .pt weights file or a model name such as
                        'yolov8n.pt'.  Ultralytics will auto-download the
                        weights on first use if they are not present locally.
            confidence: Minimum confidence threshold (0.0 – 1.0).
        """
        self.confidence = confidence
        logger.info(
            "Loading YOLO model from '%s' (confidence=%.2f) …", model_path, confidence
        )
        # PyTorch >= 2.6 changed the default of `weights_only` in
        # `torch.load` from False to True, which breaks ultralytics < 8.3
        # because the .pt file contains arbitrary Python classes.
        # Register the known ultralytics globals so the safe-unpickler
        # allows them without having to drop back to weights_only=False.
        try:
            from ultralytics.nn.tasks import (  # noqa: PLC0415
                ClassificationModel,
                DetectionModel,
                PoseModel,
                SegmentationModel,
                WorldModel,
            )

            torch.serialization.add_safe_globals(
                [
                    DetectionModel,
                    SegmentationModel,
                    PoseModel,
                    ClassificationModel,
                    WorldModel,
                ]
            )
        except (ImportError, AttributeError):
            # Older ultralytics builds may not have all of these classes;
            # add whichever are available and ignore the rest.
            _candidate_classes = [
                "ultralytics.nn.tasks.DetectionModel",
                "ultralytics.nn.tasks.SegmentationModel",
                "ultralytics.nn.tasks.PoseModel",
                "ultralytics.nn.tasks.ClassificationModel",
            ]
            import importlib  # noqa: PLC0415

            _safe: list = []
            for _cls_path in _candidate_classes:
                _mod_name, _cls_name = _cls_path.rsplit(".", 1)
                try:
                    _mod = importlib.import_module(_mod_name)
                    _safe.append(getattr(_mod, _cls_name))
                except (ImportError, AttributeError):
                    pass
            if _safe:
                torch.serialization.add_safe_globals(_safe)

        self.model = YOLO(model_path)
        logger.info("YOLO model loaded successfully.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict]]:
        """Run inference on *frame* and return an annotated copy plus metadata.

        Args:
            frame: BGR image as a NumPy array (H × W × 3).

        Returns:
            A 2-tuple of:
              - annotated_frame: A copy of *frame* with bounding boxes drawn.
              - detections: A list of dicts, each with keys
                  ``x1``, ``y1``, ``x2``, ``y2``, ``confidence`` (all floats).
        """
        annotated = frame.copy()
        detections: list[dict] = []

        results = self.model(
            frame,
            conf=self.confidence,
            classes=[PERSON_CLASS_ID],
            verbose=False,
        )

        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue

            for box in boxes:
                class_id = int(box.cls[0])
                if class_id != PERSON_CLASS_ID:
                    continue

                conf = float(box.conf[0])
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])

                # Record detection metadata
                detections.append(
                    {
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "confidence": round(conf, 4),
                    }
                )

                # Draw bounding box
                cv2.rectangle(annotated, (x1, y1), (x2, y2), BOX_COLOR, BOX_THICKNESS)

                # Build label string
                label = f"Person {conf * 100:.1f}%"
                self._draw_label(annotated, label, x1, y1)

        return annotated, detections

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _draw_label(
        self,
        frame: np.ndarray,
        label: str,
        box_x1: int,
        box_y1: int,
    ) -> None:
        """Draw a filled-background label above the bounding box corner."""
        (text_w, text_h), baseline = cv2.getTextSize(
            label, LABEL_FONT, LABEL_FONT_SCALE, LABEL_FONT_THICKNESS
        )

        # Position the label so it sits just above the box top edge.
        # If the box is at the very top of the frame, place the label inside
        # the box instead so it stays visible.
        label_y_bottom = box_y1 - LABEL_PADDING
        label_y_top = label_y_bottom - text_h - LABEL_PADDING

        if label_y_top < 0:
            # Flip inside the box
            label_y_top = box_y1 + LABEL_PADDING
            label_y_bottom = label_y_top + text_h + LABEL_PADDING

        bg_x1 = box_x1
        bg_y1 = label_y_top
        bg_x2 = box_x1 + text_w + LABEL_PADDING * 2
        bg_y2 = label_y_bottom + baseline

        # Clamp to frame boundaries
        h, w = frame.shape[:2]
        bg_x2 = min(bg_x2, w - 1)
        bg_y1 = max(bg_y1, 0)

        # Filled rectangle background
        cv2.rectangle(frame, (bg_x1, bg_y1), (bg_x2, bg_y2), LABEL_BG_COLOR, cv2.FILLED)

        # Text on top of background
        text_origin = (box_x1 + LABEL_PADDING, label_y_bottom)
        cv2.putText(
            frame,
            label,
            text_origin,
            LABEL_FONT,
            LABEL_FONT_SCALE,
            LABEL_TEXT_COLOR,
            LABEL_FONT_THICKNESS,
            cv2.LINE_AA,
        )
