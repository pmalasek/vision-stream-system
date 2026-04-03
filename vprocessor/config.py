import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    RTSP_URL: str = os.getenv("RTSP_URL", "rtsp://localhost:8554/live")
    # Force TCP transport by default to avoid UDP packet-loss artefacts
    # (H.264 corrupted-macroblock warnings from FFmpeg/OpenCV).
    # Set RTSP_TRANSPORT=udp in the environment to revert to UDP.
    RTSP_TRANSPORT: str = os.getenv("RTSP_TRANSPORT", "tcp")
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "./output")
    YOLO_MODEL: str = os.getenv("YOLO_MODEL", "yolov8n.pt")
    CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.5"))
    PORT: int = int(os.getenv("PORT", "8000"))
    HOST: str = os.getenv("HOST", "0.0.0.0")


config = Config()
