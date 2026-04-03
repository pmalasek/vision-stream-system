#!/usr/bin/env python3
"""Minimalistický YOLO worker pro vProcFast.

Komunikační protokol přes stdin/stdout:
- stdin: opakovaně snímky jako 4B big-endian délka + JPEG payload
- stdout: 1 JSON řádek na snímek: {"detections": [...]} nebo {"error": "..."}

Skript je navržený jako dlouho běžící subprocess, který Go backend krmí
snímky a očekává zpět detekce osob.
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
    """Vrátí existující cestu k modelu, pokud ji umíme dohledat.

    Podporuje absolutní i relativní cesty. U relativních cest zkouší:
    1) root repozitáře,
    2) adresář vprocessor,
    3) aktuální pracovní adresář procesu.
    """
    # Nejprve zkusíme, zda uživatel neposlal validní absolutní cestu.
    path = Path(value)
    if path.is_absolute() and path.exists():
        return str(path)

    # U relativní cesty zkoušíme několik známých kořenů projektu.
    candidates = [
        REPO_ROOT / value,
        VPROCESSOR_DIR / value,
        Path.cwd() / value,
    ]
    # Vrátíme první existující kandidát.
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # Fallback: vrátíme původní hodnotu, případné selhání řeší volající.
    return value


def _read_exact(stream, length: int) -> bytes:
    """Načte přesně `length` bajtů nebo vyhodí EOFError.

    Důležité pro binární framing: `read` může vrátit méně dat, než požadujeme.
    """
    data = bytearray()
    # Čteme opakovaně, dokud nemáme přesně požadovaný počet bajtů.
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            # EOF uprostřed rámce = poškozený/nedokončený přenos.
            raise EOFError("unexpected EOF while reading frame")
        data.extend(chunk)
    return bytes(data)


def _emit(payload: dict) -> None:
    """Pošle jednu JSON zprávu na stdout a okamžitě flushne buffer."""
    # separators minimalizují velikost zprávy (bez zbytečných mezer).
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    # Flush je nutný, aby Go proces dostal odpověď bez zpoždění.
    sys.stdout.flush()


def main() -> int:
    """Hlavní smyčka workeru.

    Načte konfiguraci z prostředí, inicializuje `PersonDetector` a pak:
    - čte framed JPEG snímky ze stdin,
    - dekóduje je přes OpenCV,
    - vrací detekce jako JSON řádky na stdout.
    """
    # Načtení runtime parametrů z prostředí.
    model_path = _resolve_model_path(os.getenv("YOLO_MODEL", "models/yolov8n.pt"))
    confidence = float(os.getenv("CONFIDENCE_THRESHOLD", "0.5"))
    inference_scale = float(os.getenv("INFERENCE_SCALE", "1.0"))
    imgsz = int(os.getenv("YOLO_IMGSZ", "640"))

    # Inicializace detektoru (model se načte jednou při startu workeru).
    detector = PersonDetector(
        model_path=model_path,
        confidence=confidence,
        inference_scale=inference_scale,
        imgsz=imgsz,
    )

    stdin = sys.stdin.buffer
    while True:
        # 4B hlavička nese délku následujícího JPEG payloadu.
        header = stdin.read(4)
        if not header:
            # Čisté EOF: nadřazený proces zavřel stdin => korektní ukončení.
            return 0
        if len(header) != 4:
            # Neúplná hlavička značí poruchu framingu.
            raise EOFError("incomplete frame header")

        # Dekódování délky rámce (unsigned big-endian uint32).
        frame_size = struct.unpack(">I", header)[0]
        # Načtení přesně celého JPEG payloadu.
        payload = _read_exact(stdin, frame_size)
        # Převod JPEG bytes -> OpenCV obraz.
        frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            # Nekorektní JPEG: vrátíme řízenou chybu, worker běží dál.
            _emit({"error": "failed to decode jpeg", "detections": []})
            continue

        # Samotná detekce osob; první návratová hodnota (annotated frame)
        # se zde nepoužívá, worker vrací pouze seznam detekcí.
        _, detections = detector.detect(frame)
        # Odpověď pro Go backend: jedna JSON řádka na jeden vstupní snímek.
        _emit({"detections": detections})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - best effort logování workeru
        print(f"worker fatal error: {exc}", file=sys.stderr, flush=True)
        raise
