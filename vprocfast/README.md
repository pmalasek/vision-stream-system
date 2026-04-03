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

## Architektura (jak to spolu mluví)

`vProcFast` je rozdělený do malých interních balíčků:

- `main.go`
  - inicializace konfigurace, Socket.IO, Processoru a HTTP serveru
  - volba běhu `synthetic` vs `rtsp`
- `internal/config`
  - načítání environment proměnných, validace, default hodnoty
- `internal/source`
  - příjem RTSP streamu přes `ffmpeg` a převod na JPEG snímky
- `internal/detection`
  - fake detekce + volitelný Python worker (YOLO inferenční backend)
- `internal/vision`
  - anotace JPEG snímků (vykreslení boxů)
- `internal/store`
  - segmentace výstupů do timestamp složek (`detections.jsonl`, volitelně `output.mp4`)
- `internal/httpapi`
  - HTTP routy (`/stream`, `/health`, `/api/...`, `/socket.io/`)
- `internal/httpx`
  - CORS helpery pro lokální vývoj
- `internal/model`
  - sdílené datové struktury eventů

## Tok dat (pipeline)

1. Zdroj snímků:
   - buď interní syntetický generátor,
   - nebo RTSP přes `ffmpeg` (`image2pipe`, `mjpeg`).
2. Detekce:
   - fake (MVP),
   - nebo Python worker (pokud `DETECTION_BACKEND=python`).
3. Vykreslení:
   - detekce se vykreslí do JPEG snímku (`internal/vision`).
4. Publikace:
   - MJPEG stream přes `/stream`,
   - JSON eventy přes Socket.IO (`detection`, `stats`).
5. Persist:
   - metadata do `detections.jsonl`,
   - volitelně video do `output.mp4`.

## Detail endpointů

- `GET /health`
  - lehký healthcheck + runtime snapshot (`status`, `uptime`, `fps`, `connected_clients`)
- `GET /api/stats`
  - vrací `StatsEvent`
- `GET /api/detections?limit=&offset=`
  - vrací stránkovanou in-memory historii `DetectionEvent`
- `GET /stream`
  - MJPEG stream (`multipart/x-mixed-replace`)
- `WS /socket.io`
  - event `detection`: jednotlivé detekční události
  - event `stats`: periodické statistiky (1 Hz)
- `POST /webrtc/offer`
  - zatím pouze placeholder (`501 Not Implemented`)

## Konfigurace detekce

### 1) Fake backend (default mimo RTSP inferenci)

- rychlý, bez externích modelů
- vhodný pro UI/API testy

### 2) Python backend

- `vProcFast` spustí Python worker skript a komunikuje přes stdin/stdout
- očekává line-oriented JSON odpovědi
- při selhání workeru se backend automaticky přepne na fake detekce (degradace bez pádu služby)

## Ukládání segmentů

- každý segment je adresář pojmenovaný timestampem:
  - `detections.jsonl` (vždy)
  - `output.mp4` (jen pokud `RECORD_OUTPUT=true`)
- po dosažení `MAX_SEGMENTS` se nejstarší segmenty mažou

## Poznámky k provozu

- CORS i Socket.IO `Origin` kontrola jsou záměrně omezené na localhost/127.0.0.1.
- `.env` je načítán pro lokální vývoj; v produkci doporučeno předávat env přes orchestrátor.
- RTSP čte ffmpeg proces, takže je nutné mít dostupný `ffmpeg` v PATH nebo nastavit `FFMPEG_PATH`.

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

## Doporučený postup při debugování

1. Ověř `GET /health` (status + fps).
2. Otevři `GET /stream` a zkontroluj, že chodí JPEG snímky.
3. Sleduj Socket.IO eventy `detection` a `stats`.
4. Při RTSP problémech nejdřív ověř `ffmpeg` a `RTSP_URL`.
5. Při Python inferenci ověř cesty `PYTHON_EXECUTABLE` + `DETECTOR_SCRIPT`.
