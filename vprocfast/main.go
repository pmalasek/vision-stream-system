package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"vprocfast/internal/config"
	"vprocfast/internal/detection"
	"vprocfast/internal/httpapi"
	"vprocfast/internal/model"
	"vprocfast/internal/source"
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

	metaFileMu sync.Mutex
	metaFile   *os.File

	sio *socketio.Server
}

func newProcessor(cfg Config, sio *socketio.Server) (*Processor, error) {
	sessionDir := filepath.Join(cfg.OutputDir, time.Now().Format("2006-01-02_15-04-05"))
	if err := os.MkdirAll(sessionDir, 0o755); err != nil {
		return nil, fmt.Errorf("failed to create output session dir: %w", err)
	}

	metaPath := filepath.Join(sessionDir, "detections.jsonl")
	metaFile, err := os.OpenFile(metaPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("failed to open metadata file: %w", err)
	}

	p := &Processor{
		cfg:       cfg,
		startedAt: time.Now(),
		history:   make([]DetectionEvent, 0, 2048),
		metaFile:  metaFile,
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
	p.metaFileMu.Lock()
	defer p.metaFileMu.Unlock()
	if p.metaFile != nil {
		_ = p.metaFile.Close()
		p.metaFile = nil
	}
}

func (p *Processor) start(ctx context.Context) {
	if p.cfg.SourceMode == "rtsp" {
		p.startRTSPLoop(ctx)
		return
	}
	p.startSynthetic(ctx)
}

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

func (p *Processor) annotateJPEG(frameID int64, jpg []byte) ([]byte, []Detection) {
	return vision.AnnotateJPEG(jpg, p.cfg.JPEGQuality, func(frameW, frameH int) []model.Detection {
		return p.detectPeople(frameID, jpg, frameW, frameH)
	})
}

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
			log.Printf("[WARN] detector worker failed: %v", err)
			return []Detection{}
		}
		return detections
	}

	return detection.FakeDetections(p.cfg, frameID, frameW, frameH)
}

func (p *Processor) publishFrame(event DetectionEvent, jpegBytes []byte) {
	p.setLatestJPEG(jpegBytes)
	p.appendHistory(event)
	p.writeJSONL(event)
	p.totalDetections.Add(int64(len(event.Detections)))
	p.sio.BroadcastToNamespace("/", "detection", event)
}

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

func (p *Processor) setLatestJPEG(j []byte) {
	p.latestJPEGMu.Lock()
	defer p.latestJPEGMu.Unlock()
	p.latestJPEG = j
	p.latestSeq++
}

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

func (p *Processor) appendHistory(evt DetectionEvent) {
	p.historyMu.Lock()
	defer p.historyMu.Unlock()
	p.history = append(p.history, evt)
	if len(p.history) > 10_000 {
		p.history = p.history[len(p.history)-10_000:]
	}
}

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

func (p *Processor) writeJSONL(evt DetectionEvent) {
	b, err := json.Marshal(evt)
	if err != nil {
		return
	}

	p.metaFileMu.Lock()
	defer p.metaFileMu.Unlock()
	if p.metaFile == nil {
		return
	}
	_, _ = p.metaFile.Write(append(b, '\n'))
}

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
