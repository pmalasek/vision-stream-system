package httpapi

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"vprocfast/internal/httpx"
	"vprocfast/internal/model"
)

type Dependencies struct {
	SocketHandler    http.Handler
	CurrentStats     func() model.StatsEvent
	GetHistory       func(limit, offset int) []model.DetectionEvent
	GetLatestJPEG    func() ([]byte, int64)
	ConnectedClients func() int64
}

// NewMux postaví HTTP routy kompatibilní s očekáváním dashboardu/vprocessor API.
//
// Endpoints:
// - /health
// - /api/stats
// - /api/detections
// - /webrtc/offer (MVP stub)
// - /stream (MJPEG)
// - /socket.io/ (pokud je předán SocketHandler)
func NewMux(deps Dependencies) *http.ServeMux {
	mux := http.NewServeMux()

	if deps.SocketHandler != nil {
		mux.Handle("/socket.io/", httpx.WrapCORSHandler(deps.SocketHandler))
	}

	mux.HandleFunc("/health", httpx.WrapCORS(func(w http.ResponseWriter, r *http.Request) {
		stats := deps.CurrentStats()
		connected := int64(0)
		if deps.ConnectedClients != nil {
			connected = deps.ConnectedClients()
		}

		_ = json.NewEncoder(w).Encode(map[string]any{
			"status":            stats.Status,
			"uptime":            stats.UptimeSeconds,
			"fps":               stats.FPS,
			"connected_clients": connected,
		})
	}))

	mux.HandleFunc("/api/stats", httpx.WrapCORS(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(deps.CurrentStats())
	}))

	mux.HandleFunc("/api/detections", httpx.WrapCORS(func(w http.ResponseWriter, r *http.Request) {
		limit := parseInt(r.URL.Query().Get("limit"))
		offset := parseInt(r.URL.Query().Get("offset"))
		_ = json.NewEncoder(w).Encode(deps.GetHistory(limit, offset))
	}))

	mux.HandleFunc("/webrtc/offer", httpx.WrapCORS(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		http.Error(w, `{"detail":"WebRTC not implemented in vProcFast MVP"}`, http.StatusNotImplemented)
	}))

	mux.HandleFunc("/stream", httpx.WrapCORS(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
		w.Header().Set("Cache-Control", "no-cache, no-store, must-revalidate")
		w.Header().Set("Pragma", "no-cache")
		w.Header().Set("Expires", "0")

		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}

		lastSeq := int64(-1)
		ticker := time.NewTicker(30 * time.Millisecond)
		defer ticker.Stop()

		for {
			select {
			case <-r.Context().Done():
				return
			case <-ticker.C:
				jpg, seq := deps.GetLatestJPEG()
				if len(jpg) == 0 || seq == lastSeq {
					continue
				}
				lastSeq = seq

				_, _ = w.Write([]byte("--frame\r\nContent-Type: image/jpeg\r\n\r\n"))
				_, _ = w.Write(jpg)
				_, _ = w.Write([]byte("\r\n"))
				flusher.Flush()
			}
		}
	}))

	return mux
}

// parseInt je tolerantní parser query parametrů.
// Při chybě vrací 0, což následně řeší validační logika volajícího.
func parseInt(v string) int {
	n, _ := strconv.Atoi(v)
	return n
}
