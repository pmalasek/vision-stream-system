# vstreamer

A Go RTSP streaming server that serves a video file as an RTSP stream on port 8554, behaving like an IP camera. It uses [gortsplib v4](https://github.com/bluenviron/gortsplib) as the RTSP server and spawns `ffmpeg` as a subprocess to decode/re-encode and publish the video.

## How It Works

1. A gortsplib v4 RTSP server starts and listens for connections.
2. `ffmpeg` is spawned as a subprocess and publishes the video to the server via RTSP (`ANNOUNCE` + `RECORD`).
3. Readers (e.g. `vprocessor`/OpenCV) connect to the server and receive the relayed stream.
4. When `ffmpeg` exits (video ends), it is automatically restarted if `--loop` is `true`.

```
Video File → ffmpeg → vstreamer (RTSP relay) → vprocessor / OpenCV
```

## Prerequisites

- Go 1.21+
- `ffmpeg` with `libx264` support installed and available in `PATH`

## Build

```sh
go mod tidy
go build -o vstreamer .
```

## Usage

### Minimal (loop enabled by default)

```sh
./vstreamer --video /path/to/video.mp4
```

### Full options

```sh
./vstreamer --video /path/to/video.mp4 --port 8554 --path live --loop true
```

### Disable looping (stream ends when video ends)

```sh
./vstreamer --video /path/to/video.mp4 --loop false
```

## Flags

| Flag      | Type   | Default | Description                                      |
|-----------|--------|---------|--------------------------------------------------|
| `--video` | string | *(required)* | Path to the input video file               |
| `--port`  | int    | `8554`  | RTSP server port                                 |
| `--path`  | string | `live`  | RTSP stream path segment                         |
| `--loop`  | bool   | `true`  | Restart ffmpeg and loop the video when it ends   |

## Connecting a Reader

Once `vstreamer` is running, any RTSP-capable client can connect:

```
rtsp://localhost:8554/live
```

### OpenCV (Python)

```python
import cv2
cap = cv2.VideoCapture("rtsp://localhost:8554/live")
```

### OpenCV (C++)

```cpp
cv::VideoCapture cap("rtsp://localhost:8554/live");
```

### FFplay

```sh
ffplay rtsp://localhost:8554/live
```

### VLC

```sh
vlc rtsp://localhost:8554/live
```

## Docker

### Build

```sh
docker build -t vstreamer .
```

### Run

```sh
docker run --rm \
  -p 8554:8554 \
  -v /path/to/videos:/videos \
  vstreamer --video /videos/sample.mp4
```

## Notes

- Audio is stripped (`-an`) — the stream is video-only H.264 in yuv420p pixel format.
- The server uses TCP transport for the ffmpeg → server leg (`-rtsp_transport tcp`).
- `ffmpeg` is encoded with `libx264 -preset ultrafast -tune zerolatency` for minimal latency.
- The server starts **before** ffmpeg is spawned (500 ms delay) to ensure it is ready to accept the ANNOUNCE.
```

Now let me run `go mod tidy` to generate the `go.sum` and resolve all transitive dependencies: