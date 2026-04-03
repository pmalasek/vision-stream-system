# vprocessor

A Python FastAPI service that ingests an RTSP video stream, detects persons using YOLOv8, and serves the annotated output as an MJPEG stream while broadcasting real-time detection metadata over Socket.IO.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Setup](#setup)
  - [Local (venv)](#local-venv)
  - [Docker](#docker)
- [Configuration](#configuration)
- [Running](#running)
  - [Local](#local)
  - [Docker Compose](#docker-compose)
- [API Reference](#api-reference)
  - [HTTP Endpoints](#http-endpoints)
  - [Socket.IO Events](#socketio-events)
- [Output Files](#output-files)
- [Project Structure](#project-structure)

---

## Overview

| Capability | Detail |
|---|---|
| Input | RTSP stream (e.g. from **vstreamer** at `rtsp://localhost:8554/live`) |
| Detection | YOLOv8n – person class only (class 0) |
| Live output | MJPEG stream at `GET /stream` |
| Real-time events | Socket.IO `detection` + `stats` events |
| Disk output | Timestamped MP4 video + JSONL metadata |

---

## Architecture

```
RTSP source
    │
    ▼
cv2.VideoCapture           (background thread via run_in_executor)
    │
    ▼
PersonDetector             (YOLOv8 inference, bounding-box drawing)
    │
    ├──► VideoRecorder     (MP4 + JSONL → output/<timestamp>/)
    │
    ├──► latest_frame      (JPEG bytes, read by /stream endpoint)
    │
    └──► Socket.IO         ("detection" and "stats" events → clients)
```

---

## Requirements

- Python 3.11+
- The packages listed in `requirements.txt`
- An RTSP source (e.g. the **vstreamer** service)

On first run ultralytics will automatically download the YOLOv8 weights file (`yolov8n.pt`, ~6 MB) if it is not already present locally.

---

## Setup

### Local (venv)

```bash
# 1. Clone / navigate to the service directory
cd vision-stream-system/vprocessor

# 2. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install --no-cache-dir -r requirements.txt

# 4. Copy and edit the environment file
cp .env.example .env
```

Edit `.env` to point `RTSP_URL` at your stream source before continuing.

### Docker

```bash
cd vision-stream-system/vprocessor
docker build -t vprocessor:latest .
```

---

## Configuration

All settings are read from environment variables (and from a `.env` file when running locally).

| Variable | Default | Description |
|---|---|---|
| `RTSP_URL` | `rtsp://localhost:8554/live` | Full RTSP URL of the input stream |
| `OUTPUT_DIR` | `./output` | Root directory for recorded sessions |
| `YOLO_MODEL` | `yolov8n.pt` | YOLOv8 weights file or model name |
| `CONFIDENCE_THRESHOLD` | `0.5` | Minimum detection confidence (0.0 – 1.0) |
| `HOST` | `0.0.0.0` | Bind address for uvicorn |
| `PORT` | `8000` | Bind port for uvicorn |

Copy `.env.example` to `.env` and adjust the values as needed:

```bash
cp .env.example .env
```

---

## Running

### Local

```bash
# Make sure your .env is configured, then:
source .venv/bin/activate
python -m uvicorn main:socket_app --host 0.0.0.0 --port 8000 --reload
```

If the RTSP source is not yet available the service will keep retrying the connection every 2 seconds and log a warning. All HTTP endpoints and Socket.IO remain available during that time.

### Docker Compose

Add the following service block to your project's `docker-compose.yml`:

```yaml
vprocessor:
  build: ./vprocessor
  ports:
    - "8000:8000"
  environment:
    RTSP_URL: rtsp://vstreamer:8554/live
    OUTPUT_DIR: /app/output
    YOLO_MODEL: yolov8n.pt
    CONFIDENCE_THRESHOLD: "0.5"
  volumes:
    - ./vprocessor/output:/app/output
  depends_on:
    - vstreamer
```

Then start the stack:

```bash
docker compose up --build
```

---

## API Reference

### HTTP Endpoints

#### `GET /stream`

MJPEG stream of the annotated video feed. Suitable for embedding directly in a browser:

```html
<img src="http://localhost:8000/stream" />
```

- **Media type:** `multipart/x-mixed-replace; boundary=frame`
- **CORS:** allowed from all origins
- **Frame rate:** up to 30 fps (async yield-based)

---

#### `GET /health`

Liveness / readiness probe.

**Response**

```json
{
  "status": "streaming",
  "uptime": 42.7,
  "fps": 24.3,
  "connected_clients": 1
}
```

Possible `status` values: `starting`, `connecting`, `streaming`, `reconnecting`, `stopped`.

---

#### `GET /api/stats`

Current processing statistics snapshot.

**Response**

```json
{
  "fps": 24.3,
  "total_frames": 1042,
  "total_detections": 318,
  "uptime_seconds": 42.7,
  "status": "streaming"
}
```

---

#### `GET /api/detections`

Read persisted detection records from the current session JSONL file.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `100` | Maximum records to return |
| `offset` | integer | `0` | Records to skip |

**Response** – array of detection records:

```json
[
  {
    "frame_id": 101,
    "timestamp": 1712345678.123,
    "person_count": 2,
    "detections": [
      { "x1": 120, "y1": 45, "x2": 310, "y2": 480, "confidence": 0.9231 },
      { "x1": 640, "y1": 80, "x2": 790, "y2": 460, "confidence": 0.8754 }
    ]
  }
]
```

---

### Socket.IO Events

Connect to `http://localhost:8000` using any Socket.IO v5 client.

#### Server → Client: `detection`

Emitted on every processed frame.

```json
{
  "frame_id": 101,
  "timestamp": 1712345678.123,
  "person_count": 1,
  "detections": [
    { "x1": 120, "y1": 45, "x2": 310, "y2": 480, "confidence": 0.9231 }
  ]
}
```

#### Server → Client: `stats`

Emitted once per second with aggregate statistics.

```json
{
  "fps": 24.3,
  "total_frames": 1042,
  "total_detections": 318,
  "uptime_seconds": 42.7,
  "status": "streaming"
}
```

#### Server → Client: `stats` (on connect)

Immediately after a client connects the server emits a `stats` event with the current snapshot so the dashboard can populate without waiting up to one second.

---

## Output Files

Each time the service starts a new recording session is created under `OUTPUT_DIR`:

```
output/
└── 2024-04-05_14-32-10/
    ├── output.mp4          ← annotated video (MP4, mp4v codec, 25 fps)
    └── detections.jsonl    ← one JSON object per line, one line per frame
```

**JSONL record format:**

```json
{"frame_id": 1, "timestamp": 1712345600.001, "person_count": 0, "detections": []}
{"frame_id": 2, "timestamp": 1712345600.041, "person_count": 1, "detections": [{"x1": 50, "y1": 30, "x2": 200, "y2": 420, "confidence": 0.8812}]}
```

---

## Project Structure

```
vprocessor/
├── config.py          # Environment-based configuration
├── detector.py        # PersonDetector – YOLOv8 inference + annotation
├── recorder.py        # VideoRecorder  – MP4 + JSONL writer
├── processor.py       # VideoProcessor – async pipeline orchestrator
├── main.py            # FastAPI app, Socket.IO server, HTTP endpoints
├── requirements.txt
├── Dockerfile
├── .env.example
├── output/            # Created automatically; holds recording sessions
└── README.md
```
