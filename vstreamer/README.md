# vstreamer

RTSP relay server napsaný v Go. Slouží k přehrávání video souboru jako RTSP streamu – chová se jako IP kamera. Jako RTSP server využívá knihovnu [gortsplib v4](https://github.com/bluenviron/gortsplib) a spouští `ffmpeg` jako podproces, který video publikuje do serveru.

## Jak to funguje

```
Video soubor → ffmpeg → vstreamer (RTSP relay) → vprocessor / OpenCV
```

1. Spustí se gortsplib RTSP server a začne naslouchat na zvoleném portu.
2. Po 500 ms se spustí `ffmpeg` jako podproces a pošle do serveru `ANNOUNCE` + `RECORD`.
3. Čtenáři (např. `vprocessor` / OpenCV) se připojí a dostávají přeposílaný stream.
4. Po skončení videa se `ffmpeg` automaticky restartuje, pokud je povoleno `--loop`.

## Požadavky

- Go 1.21+
- `ffmpeg` s podporou `libx264` dostupný v `PATH`

## Sestavení a spuštění

```sh
go mod tidy
go build -o vstreamer .
./vstreamer --video /cesta/k/videu.mp4
```

## Příznaky CLI

| Příznak | Typ | Výchozí | Popis |
|---------|-----|---------|-------|
| `--video` | string | *(povinné)* | Cesta k vstupnímu video souboru |
| `--port` | int | `8554` | Port RTSP serveru |
| `--udp-rtp` | int | `8000` | UDP port pro RTP pakety (0 = zakázat UDP) |
| `--udp-rtcp` | int | `8001` | UDP port pro RTCP pakety (0 = zakázat UDP) |
| `--path` | string | `live` | Segment cesty RTSP streamu |
| `--loop` | bool | `true` | Opakování videa po skončení |

### Příklady

Minimální spuštění (smyčka povolena ve výchozím stavu):

```sh
./vstreamer --video /cesta/k/videu.mp4
```

Vypnutí smyčky (stream skončí spolu s videem):

```sh
./vstreamer --video /cesta/k/videu.mp4 --loop false
```

Vlastní porty a cesta:

```sh
./vstreamer --video /cesta/k/videu.mp4 --port 8554 --udp-rtp 8556 --udp-rtcp 8557 --path live
```

## ffmpeg argumenty

`buildFFmpegArgs` sestaví následující příkaz:

```sh
ffmpeg -re -stream_loop -1 -i <soubor> \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -pix_fmt yuv420p -an \
  -rtsp_transport tcp -f rtsp rtsp://127.0.0.1:<port>/<path>
```

- `-re` – čte vstup v reálném čase (simulace kamery)
- `-stream_loop -1` – nekonečná smyčka (jen pokud `--loop=true`)
- `-an` – audio je odstraněno, stream je pouze video (H.264, yuv420p)
- `-rtsp_transport tcp` – transport ffmpeg → server probíhá přes TCP

## UDP transport

Ve výchozím stavu jsou UDP porty nastaveny na `8000` (RTP) a `8001` (RTCP). Oba porty musí být nenulové, aby byl UDP povolen; jinak server přijímá pouze TCP připojení.

> **Poznámka:** V `docker-compose.yml` jsou UDP porty přepsány na `8556`/`8557`, aby nedocházelo ke kolizím s jiným softwarem.

## Připojení čtenáře

Po spuštění `vstreamer` se k němu může připojit jakýkoli RTSP klient:

```
rtsp://localhost:8554/live
```

### OpenCV (Python)

```python
import cv2
cap = cv2.VideoCapture("rtsp://localhost:8554/live")
```

### ffplay

```sh
ffplay rtsp://localhost:8554/live
```

### VLC (UDP – výchozí)

```sh
vlc rtsp://localhost:8554/live
```

### VLC (TCP)

```sh
vlc --rtsp-tcp rtsp://localhost:8554/live
```

## Docker

### Sestavení

```sh
docker build -t vstreamer .
```

### Spuštění

```sh
docker run --rm \
  -p 8554:8554 \
  -p 8556:8556/udp \
  -p 8557:8557/udp \
  -v /cesta/k/videam:/videos \
  vstreamer --video /videos/sample.mp4 --udp-rtp 8556 --udp-rtcp 8557
```

## Struktura projektu

```
vstreamer/
├── main.go        # celá implementace
├── go.mod
├── go.sum
└── Dockerfile
```

## Poznámky

- **WriteQueueSize** je nastaven na `1024` (výchozí hodnota gortsplib je 256). Při ~30 fps a typické H.264 fragmentaci (~5 RTP paketů/snímek) to poskytuje přibližně 6 sekund rezervy místo původních ~1,7 s.
- **Rate-limiting chyb zápisu:** pokud pomalý čtenář (např. vprocessor blokovaný YOLO inferencí) nestíhá odebírat pakety, gortsplib volá `OnStreamWriteError` pro každý zahozený paket. Aby byl log čitelný, zaznamenává se souhrnná hláška nejvýše jednou za 5 sekund.
- Leg ffmpeg → server vždy probíhá přes TCP (`-rtsp_transport tcp`). UDP transport se týká pouze připojení čtenářů.
- Server se spustí před `ffmpeg` a čeká 500 ms, aby byl připraven přijmout `ANNOUNCE`.
- Graceful shutdown je zajištěn zachycením signálů `SIGINT` a `SIGTERM`.