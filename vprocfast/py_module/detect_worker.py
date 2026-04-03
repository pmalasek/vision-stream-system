#!/usr/bin/env python3
"""Minimal YOLO detection worker for vProcFast.

Protocol on stdin/stdout:
- stdin: repeated frames encoded as 4-byte big-endian length + JPEG bytes
- stdout: one JSON line per frame: {"detections": [...]} or {"error": "..."}
"""

from __future__ import annotations

import json
import importlib
import os
import struct
import sys
from pathlib import Path

import cv2
import numpy as np

# detect_worker.py now lives in vprocfast/py_module/, so repository root is
# three levels up: <repo>/vprocfast/py_module/detect_worker.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VPROCESSOR_DIR = REPO_ROOT / "vprocessor"
if str(VPROCESSOR_DIR) not in sys.path:
    sys.path.insert(0, str(VPROCESSOR_DIR))

PersonDetector = importlib.import_module("detector").PersonDetector


def _resolve_model_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() and path.exists():
        return str(path)

    candidates = [
        REPO_ROOT / value,
        VPROCESSOR_DIR / value,
        Path.cwd() / value,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return value


def _read_exact(stream, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise EOFError("unexpected EOF while reading frame")
        data.extend(chunk)
    return bytes(data)


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    model_path = _resolve_model_path(os.getenv("YOLO_MODEL", "models/yolov8n.pt"))
    confidence = float(os.getenv("CONFIDENCE_THRESHOLD", "0.5"))
    inference_scale = float(os.getenv("INFERENCE_SCALE", "1.0"))
    imgsz = int(os.getenv("YOLO_IMGSZ", "640"))

    detector = PersonDetector(
        model_path=model_path,
        confidence=confidence,
        inference_scale=inference_scale,
        imgsz=imgsz,
    )

    stdin = sys.stdin.buffer
    while True:
        header = stdin.read(4)
        if not header:
            return 0
        if len(header) != 4:
            raise EOFError("incomplete frame header")

        frame_size = struct.unpack(">I", header)[0]
        payload = _read_exact(stdin, frame_size)
        frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            _emit({"error": "failed to decode jpeg", "detections": []})
            continue

        _, detections = detector.detect(frame)
        _emit({"detections": detections})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - best effort worker logging
        print(f"worker fatal error: {exc}", file=sys.stderr, flush=True)
        raise
