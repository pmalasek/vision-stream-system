# vProcFast

`vProcFast` je lehký Go backend s kompatibilním API vůči `vprocessor` pro rychlé testování UI/streaming pipeline.

Vznikl primárně jako **testovací prostředí pro spolupráci Go a Pythonu**: umožňuje rychle iterovat nad API, streamováním a datovým tokem v Go, zatímco Python část (`vprocessor`, YOLO) může běžet samostatně a postupně se napojovat přes stejný kontrakt.

## Co umí (MVP)

- `GET /stream` – MJPEG stream (syntetické snímky)
- `GET /health` – health endpoint
- `GET /api/stats` – průběžné statistiky
- `GET /api/detections` – historie detekcí
- `WS /socket.io` – Socket.IO události `detection` a `stats`
- `POST /webrtc/offer` – vrací `501` (zatím neimplementováno)

## Režimy zdroje videa

- `SOURCE_MODE=synthetic` (default) – generuje interní testovací snímky
- `SOURCE_MODE=rtsp` – čte RTSP přes `ffmpeg` a publikuje MJPEG + metadata

Příklad RTSP režimu:

1. `SOURCE_MODE=rtsp`
2. `RTSP_URL=rtsp://localhost:8554/live`
3. `RTSP_TRANSPORT=tcp`

> Poznámka: tato první verze používá **syntetická data** (fake person detections), aby šel backend okamžitě spustit bez nativních závislostí (OpenCV/ONNX). Další krok je napojení na RTSP + ONNX YOLO.

## Spuštění lokálně

1. `go mod tidy`
2. `go run .`
3. Otevři `http://localhost:8001/health`

## Proměnné prostředí

- `HOST` (default `0.0.0.0`)
- `PORT` (default `8001`)
- `OUTPUT_DIR` (default `./output`)
- `FPS` (default `10`)
- `SOURCE_MODE` (`synthetic` | `rtsp`, default `synthetic`)
- `RTSP_URL` (default `rtsp://vstreamer:8554/live`)
- `RTSP_TRANSPORT` (`tcp` | `udp`, default `tcp`)
- `FFMPEG_PATH` (default `ffmpeg`)
- `JPEG_QUALITY` (30–95, default `80`)
- `CONFIDENCE_THRESHOLD` (default `0.5`)
- `ENABLE_DETECTION` (default `true`)
