package store

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"vprocfast/internal/config"
	"vprocfast/internal/model"
)

type SegmentRecorder struct {
	cfg             config.Config
	segmentDuration time.Duration
	maxSegments     int
	recordOutput    bool

	mu sync.Mutex

	segmentStart time.Time
	sessionDir   string

	metadataFile *os.File
	ffmpegCmd    *exec.Cmd
	ffmpegStdin  io.WriteCloser
}

// NewSegmentRecorder vytvoří recorder, založí output adresář a otevře první segment.
// Segmenty rotují po čase a staré se průběžně promazávají dle nastavení.
func NewSegmentRecorder(cfg config.Config) (*SegmentRecorder, error) {
	if err := os.MkdirAll(cfg.OutputDir, 0o755); err != nil {
		return nil, fmt.Errorf("create output dir: %w", err)
	}

	durMin := cfg.SegmentDurationMinutes
	if durMin <= 0 {
		durMin = 10
	}
	maxSeg := cfg.MaxSegments
	if maxSeg <= 0 {
		maxSeg = 3
	}

	r := &SegmentRecorder{
		cfg:             cfg,
		segmentDuration: time.Duration(durMin) * time.Minute,
		maxSegments:     maxSeg,
		recordOutput:    cfg.RecordOutput,
	}

	if err := r.rotateToNewSegmentLocked(time.Now()); err != nil {
		return nil, err
	}
	r.pruneOldSegmentsLocked()
	return r, nil
}

// Write uloží metadata detekce a volitelně přidá JPEG snímek do výstupního videa.
// Při překročení segment duration provede atomickou rotaci na nový segment.
func (r *SegmentRecorder) Write(event model.DetectionEvent, annotatedJPEG []byte) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if time.Since(r.segmentStart) >= r.segmentDuration {
		if err := r.rotateToNewSegmentLocked(time.Now()); err != nil {
			log.Printf("[WARN] recorder rotate failed: %v", err)
		} else {
			r.pruneOldSegmentsLocked()
		}
	}

	if r.metadataFile != nil {
		if b, err := json.Marshal(event); err == nil {
			_, _ = r.metadataFile.Write(append(b, '\n'))
			_ = r.metadataFile.Sync()
		}
	}

	if r.recordOutput && r.ffmpegStdin != nil && len(annotatedJPEG) > 0 {
		if _, err := r.ffmpegStdin.Write(annotatedJPEG); err != nil {
			log.Printf("[WARN] recorder video write failed: %v", err)
			r.closeVideoWriterLocked()
		}
	}
}

// Close korektně ukončí recorder (soubor metadat i ffmpeg writer).
func (r *SegmentRecorder) Close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closeCurrentSegmentLocked()
}

// rotateToNewSegmentLocked uzavře starý segment a otevře nový.
// Volá se výhradně pod r.mu lockem.
func (r *SegmentRecorder) rotateToNewSegmentLocked(now time.Time) error {
	r.closeCurrentSegmentLocked()

	timestamp := now.Format("2006-01-02_15-04-05")
	sessionDir := filepath.Join(r.cfg.OutputDir, timestamp)
	if err := os.MkdirAll(sessionDir, 0o755); err != nil {
		return fmt.Errorf("create segment dir: %w", err)
	}

	metaPath := filepath.Join(sessionDir, "detections.jsonl")
	meta, err := os.OpenFile(metaPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("open metadata file: %w", err)
	}

	r.sessionDir = sessionDir
	r.segmentStart = now
	r.metadataFile = meta

	if r.recordOutput {
		if err := r.startVideoWriterLocked(filepath.Join(sessionDir, "output.mp4")); err != nil {
			log.Printf("[WARN] recorder video writer disabled for segment %s: %v", sessionDir, err)
		}
	}

	log.Printf("vProcFast segment started: %s", sessionDir)
	return nil
}

// startVideoWriterLocked spustí ffmpeg proces pro převod MJPEG streamu do MP4.
// Volá se výhradně pod r.mu lockem.
func (r *SegmentRecorder) startVideoWriterLocked(videoPath string) error {
	args := []string{
		"-hide_banner",
		"-loglevel", "error",
		"-y",
		"-f", "mjpeg",
		"-r", fmt.Sprintf("%d", r.cfg.FPS),
		"-i", "-",
		"-an",
		"-c:v", "libx264",
		"-pix_fmt", "yuv420p",
		videoPath,
	}

	cmd := exec.Command(r.cfg.FFmpegPath, args...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("ffmpeg stdin pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return fmt.Errorf("ffmpeg stderr pipe: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start ffmpeg writer: %w", err)
	}

	go func() {
		s := bufio.NewScanner(stderr)
		for s.Scan() {
			line := strings.TrimSpace(s.Text())
			if line != "" {
				log.Printf("[recorder] %s", line)
			}
		}
	}()

	r.ffmpegCmd = cmd
	r.ffmpegStdin = stdin
	return nil
}

// closeVideoWriterLocked uzavře ffmpeg stdin a počká na ukončení procesu.
// Volá se výhradně pod r.mu lockem.
func (r *SegmentRecorder) closeVideoWriterLocked() {
	if r.ffmpegStdin != nil {
		_ = r.ffmpegStdin.Close()
		r.ffmpegStdin = nil
	}
	if r.ffmpegCmd != nil {
		_ = r.ffmpegCmd.Wait()
		r.ffmpegCmd = nil
	}
}

// closeCurrentSegmentLocked uzavře prostředky aktivního segmentu.
// Volá se výhradně pod r.mu lockem.
func (r *SegmentRecorder) closeCurrentSegmentLocked() {
	r.closeVideoWriterLocked()
	if r.metadataFile != nil {
		_ = r.metadataFile.Close()
		r.metadataFile = nil
	}
}

// pruneOldSegmentsLocked smaže nejstarší segmenty tak, aby zůstalo maxSegments.
// Aktivní segment je chráněný proti smazání.
func (r *SegmentRecorder) pruneOldSegmentsLocked() {
	entries, err := os.ReadDir(r.cfg.OutputDir)
	if err != nil {
		log.Printf("[WARN] recorder prune list failed: %v", err)
		return
	}

	dirs := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			dirs = append(dirs, filepath.Join(r.cfg.OutputDir, e.Name()))
		}
	}
	sort.Strings(dirs)

	toDelete := len(dirs) - r.maxSegments
	if toDelete <= 0 {
		return
	}

	for i := 0; i < toDelete; i++ {
		if dirs[i] == r.sessionDir {
			continue
		}
		if err := os.RemoveAll(dirs[i]); err != nil {
			log.Printf("[WARN] recorder prune failed for %s: %v", dirs[i], err)
		}
	}
}
