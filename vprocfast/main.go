package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"vprocfast/internal/config"
	"vprocfast/internal/detection"
	"vprocfast/internal/httpapi"
	"vprocfast/internal/model"
	"vprocfast/internal/source"
	"vprocfast/internal/store"
	"vprocfast/internal/util"
	"vprocfast/internal/vision"

	socketio "github.com/googollee/go-socket.io"
	engineio "github.com/googollee/go-socket.io/engineio"
	"github.com/googollee/go-socket.io/engineio/transport"
	pollingtransport "github.com/googollee/go-socket.io/engineio/transport/polling"
	websockettransport "github.com/googollee/go-socket.io/engineio/transport/websocket"
	"github.com/joho/godotenv"
)

type Config = config.Config
type Detection = model.Detection
type DetectionEvent = model.DetectionEvent
type StatsEvent = model.StatsEvent

// Processor je hlavní orchestrace běhu backendu.
//
// Odpovídá za:
// - příjem/produkci snímků (synteticky nebo z RTSP),
// - běh detekce (fake nebo Python worker),
// - publikaci eventů do Socket.IO,
// - udržování in-memory historie,
// - průběžné ukládání segmentů a metadat přes recorder.
type Processor struct {
	cfg      Config
	detector *detection.Worker

	startedAt time.Time
	status    atomic.Value

	frameCount      atomic.Int64
	totalDetections atomic.Int64

	latestJPEGMu sync.RWMutex
	latestJPEG   []byte
	latestSeq    int64

	historyMu sync.RWMutex
	history   []DetectionEvent

	recorder *store.SegmentRecorder

	sio *socketio.Server
}

func newProcessor(cfg Config, sio *socketio.Server) (*Processor, error) {
	recorder, err := store.NewSegmentRecorder(cfg)
	if err != nil {
		return nil, fmt.Errorf("failed to initialize segment recorder: %w", err)
	}

	p := &Processor{
		cfg:       cfg,
		startedAt: time.Now(),
		history:   make([]DetectionEvent, 0, 2048),
		recorder:  recorder,
		sio:       sio,
	}
	if cfg.EnableDetection && cfg.SourceMode == "rtsp" && cfg.DetectionBackend == "python" {
		detector, err := detection.NewWorker(cfg)
		if err != nil {
			log.Printf("[WARN] failed to start Python detector worker: %v; falling back to fake detections", err)
		} else {
			p.detector = detector
			log.Printf("vProcFast detection backend: python (%s)", cfg.YOLOModel)
		}
	} else if cfg.EnableDetection {
		log.Printf("vProcFast detection backend: %s", cfg.DetectionBackend)
	}
	p.status.Store("starting")
	return p, nil
}

func (p *Processor) close() {
	if p.detector != nil {
		p.detector.Close()
	}
	if p.recorder != nil {
		p.recorder.Close()
		p.recorder = nil
	}
}

// start spustí hlavní smyčku podle zvoleného zdroje videa.
func (p *Processor) start(ctx context.Context) {
	if p.cfg.SourceMode == "rtsp" {
		p.startRTSPLoop(ctx)
		return
	}
	p.startSynthetic(ctx)
}

// startSynthetic generuje periodicky testovací snímky i detekční události.
// Používá se jako výchozí režim pro rychlý vývoj bez externích závislostí.
func (p *Processor) startSynthetic(ctx context.Context) {
	p.status.Store("streaming")
	frameInterval := time.Second / time.Duration(p.cfg.FPS)
	ticker := time.NewTicker(frameInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			p.status.Store("stopped")
			return
		case t := <-ticker.C:
			frameID := p.frameCount.Add(1)
			unixTs := float64(t.UnixNano()) / 1_000_000_000.0

			detections := detection.FakeDetections(p.cfg, frameID, 1280, 720)
			event := DetectionEvent{
				FrameID:     frameID,
				Timestamp:   unixTs,
				PersonCount: len(detections),
				Detections:  detections,
			}

			jpegBytes := vision.BuildSyntheticFrameJPEG(frameID, event, p.cfg.JPEGQuality)
			p.publishFrame(event, jpegBytes)
		}
	}
}

// startRTSPLoop drží spojení na RTSP zdroj, čte snímky přes ffmpeg a při chybě
// se pokouší o reconnect.
func (p *Processor) startRTSPLoop(ctx context.Context) {
	if strings.TrimSpace(p.cfg.RTSPURL) == "" {
		log.Printf("[WARN] SOURCE_MODE=rtsp but RTSP_URL is empty; falling back to synthetic")
		p.startSynthetic(ctx)
		return
	}

	for {
		select {
		case <-ctx.Done():
			p.status.Store("stopped")
			return
		default:
		}

		if p.frameCount.Load() == 0 {
			p.status.Store("connecting")
		} else {
			p.status.Store("reconnecting")
		}

		err := source.ConsumeRTSP(ctx, source.RTSPConsumerOptions{
			FFmpegPath:    p.cfg.FFmpegPath,
			RTSPTransport: p.cfg.RTSPTransport,
			RTSPURL:       p.cfg.RTSPURL,
			FPS:           p.cfg.FPS,
			OnStarted: func() {
				p.status.Store("streaming")
			},
		}, func(jpg []byte) error {
			frameID := p.frameCount.Add(1)
			now := float64(time.Now().UnixNano()) / 1_000_000_000.0

			annotatedJPEG, detections := p.annotateJPEG(frameID, jpg)
			event := DetectionEvent{
				FrameID:     frameID,
				Timestamp:   now,
				PersonCount: len(detections),
				Detections:  detections,
			}
			p.publishFrame(event, annotatedJPEG)
			return nil
		})
		if err != nil && ctx.Err() == nil {
			log.Printf("[WARN] RTSP consumer stopped: %v", err)
		}

		select {
		case <-ctx.Done():
			p.status.Store("stopped")
			return
		case <-time.After(1 * time.Second):
		}
	}
}

// annotateJPEG vyhodnotí detekce nad JPEG snímkem a vykreslí bounding boxy.
func (p *Processor) annotateJPEG(frameID int64, jpg []byte) ([]byte, []Detection) {
	return vision.AnnotateJPEG(jpg, p.cfg.JPEGQuality, func(frameW, frameH int) []model.Detection {
		return p.detectPeople(frameID, jpg, frameW, frameH)
	})
}

// detectPeople vrací detekce osob pro daný snímek.
//
// Logika:
// - respektuje globální ENABLE_DETECTION,
// - umí řidší detekci přes DETECT_EVERY_N,
// - preferuje Python backend, při jeho selhání fallback na fake detekce.
func (p *Processor) detectPeople(frameID int64, jpg []byte, frameW, frameH int) []Detection {
	if !p.cfg.EnableDetection {
		return []Detection{}
	}

	detectEveryN := util.MaxInt(1, p.cfg.DetectEveryN)
	doDetect := ((frameID - 1) % int64(detectEveryN)) == 0
	if !doDetect {
		return []Detection{}
	}

	if p.detector != nil {
		detections, err := p.detector.DetectJPEG(jpg)
		if err != nil {
			log.Printf("[WARN] detector worker failed: %v; disabling python detector and falling back to fake detections", err)
			p.detector.Close()
			p.detector = nil
			return detection.FakeDetections(p.cfg, frameID, frameW, frameH)
		}
		return detections
	}

	return detection.FakeDetections(p.cfg, frameID, frameW, frameH)
}

// publishFrame aktualizuje interní stav a publikuje detection event klientům.
func (p *Processor) publishFrame(event DetectionEvent, jpegBytes []byte) {
	p.setLatestJPEG(jpegBytes)
	p.appendHistory(event)
	if p.recorder != nil {
		p.recorder.Write(event, jpegBytes)
	}
	p.totalDetections.Add(int64(len(event.Detections)))
	p.sio.BroadcastToNamespace("/", "detection", event)
}

// startStatsEmitter vysílá periodické statistiky přes Socket.IO (1x za sekundu).
func (p *Processor) startStatsEmitter(ctx context.Context) {
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			stats := p.currentStats()
			p.sio.BroadcastToNamespace("/", "stats", stats)
		}
	}
}

// currentStats sestaví snapshot provozních metrik backendu.
func (p *Processor) currentStats() StatsEvent {
	uptime := time.Since(p.startedAt).Seconds()
	if uptime <= 0 {
		uptime = 0.001
	}
	frames := p.frameCount.Load()
	fps := float64(frames) / uptime

	status, _ := p.status.Load().(string)
	if status == "" {
		status = "starting"
	}

	return StatsEvent{
		FPS:             math.Round(fps*10) / 10,
		TotalFrames:     frames,
		TotalDetections: p.totalDetections.Load(),
		UptimeSeconds:   math.Round(uptime*100) / 100,
		Status:          status,
	}
}

// setLatestJPEG atomicky uloží poslední JPEG snímek dostupný pro MJPEG stream.
func (p *Processor) setLatestJPEG(j []byte) {
	p.latestJPEGMu.Lock()
	defer p.latestJPEGMu.Unlock()
	p.latestJPEG = j
	p.latestSeq++
}

// getLatestJPEG vrací kopii posledního JPEG snímku a jeho sekvenční číslo.
// Kopie je záměrně deep-copy, aby volající neovlivnil interní buffer.
func (p *Processor) getLatestJPEG() ([]byte, int64) {
	p.latestJPEGMu.RLock()
	defer p.latestJPEGMu.RUnlock()
	if p.latestJPEG == nil {
		return nil, p.latestSeq
	}
	cpy := make([]byte, len(p.latestJPEG))
	copy(cpy, p.latestJPEG)
	return cpy, p.latestSeq
}

// appendHistory přidá event do historie s omezením na fixní velikost.
func (p *Processor) appendHistory(evt DetectionEvent) {
	p.historyMu.Lock()
	defer p.historyMu.Unlock()
	p.history = append(p.history, evt)
	if len(p.history) > 10_000 {
		p.history = p.history[len(p.history)-10_000:]
	}
}

// getHistory vrátí stránkovaný výřez historie detekcí.
// Hodnoty limit/offset jsou defensivně normalizovány.
func (p *Processor) getHistory(limit, offset int) []DetectionEvent {
	if limit < 1 {
		limit = 100
	}
	if limit > 1000 {
		limit = 1000
	}
	if offset < 0 {
		offset = 0
	}

	p.historyMu.RLock()
	defer p.historyMu.RUnlock()

	if offset >= len(p.history) {
		return []DetectionEvent{}
	}

	end := offset + limit
	if end > len(p.history) {
		end = len(p.history)
	}

	out := make([]DetectionEvent, end-offset)
	copy(out, p.history[offset:end])
	return out
}

// main inicializuje konfiguraci, Socket.IO server, processor a HTTP API.
//
// Poznámka: .env se načítá pouze pro lokální DX; v kontejneru se očekává,
// že proměnné dodá orchestrátor (např. Docker Compose).
func main() {
	// Lokální UX: načti .env z aktuálního adresáře nebo z parent rootu repozitáře.
	// V Docker Compose jsou proměnné předány přímo prostředím, tam to ničemu nevadí.
	_ = godotenv.Load()
	_ = godotenv.Load("../.env")

	cfg := config.Load()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var connectedClients atomic.Int64

	wsTransport := &websockettransport.Transport{
		CheckOrigin: func(r *http.Request) bool {
			origin := strings.TrimSpace(r.Header.Get("Origin"))
			if origin == "" {
				// Non-browser clients (ffplay/tests) obvykle Origin neposílají.
				return true
			}

			// Lokální vývoj (Vite, preview, případně backend na jiném portu localhost).
			return strings.HasPrefix(origin, "http://localhost:") ||
				strings.HasPrefix(origin, "http://127.0.0.1:") ||
				strings.HasPrefix(origin, "https://localhost:") ||
				strings.HasPrefix(origin, "https://127.0.0.1:")
		},
	}

	sio := socketio.NewServer(&engineio.Options{
		Transports: []transport.Transport{
			pollingtransport.Default,
			wsTransport,
		},
	})
	sio.OnConnect("/", func(s socketio.Conn) error {
		s.SetContext("")
		connectedClients.Add(1)
		log.Printf("[socket.io] client connected: %s", s.ID())
		return nil
	})
	sio.OnError("/", func(s socketio.Conn, err error) {
		log.Printf("[socket.io] error: %v", err)
	})
	sio.OnDisconnect("/", func(s socketio.Conn, reason string) {
		connectedClients.Add(-1)
		log.Printf("[socket.io] client disconnected: %s (%s)", s.ID(), reason)
	})
	go func() {
		if err := sio.Serve(); err != nil {
			log.Fatalf("socket.io listen error: %v", err)
		}
	}()
	defer sio.Close()

	processor, err := newProcessor(cfg, sio)
	if err != nil {
		log.Fatalf("failed to create processor: %v", err)
	}
	defer processor.close()

	log.Printf("vProcFast source mode: %s", cfg.SourceMode)
	if cfg.SourceMode == "rtsp" {
		log.Printf("vProcFast RTSP source: %s (%s)", cfg.RTSPURL, cfg.RTSPTransport)
	}

	go processor.start(ctx)
	go processor.startStatsEmitter(ctx)

	mux := httpapi.NewMux(httpapi.Dependencies{
		SocketHandler:    sio,
		CurrentStats:     processor.currentStats,
		GetHistory:       processor.getHistory,
		GetLatestJPEG:    processor.getLatestJPEG,
		ConnectedClients: connectedClients.Load,
	})

	addr := fmt.Sprintf("%s:%d", cfg.Host, cfg.Port)
	log.Printf("vProcFast listening on http://%s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
