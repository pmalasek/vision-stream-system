package detection

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"os/exec"
	"strings"
	"sync"

	"vprocfast/internal/config"
	"vprocfast/internal/model"
	"vprocfast/internal/util"
)

type Worker struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader
	mu     sync.Mutex
}

type workerResponse struct {
	Detections []model.Detection `json:"detections"`
	Error      string            `json:"error"`
}

// NewWorker spustí Python proces pro inferenci a připraví pipes pro IPC.
//
// Protokol:
// - vstup: [4-byte big-endian délka][JPEG payload]
// - výstup: jedna JSON řádka s polem detections nebo chybou.
func NewWorker(cfg config.Config) (*Worker, error) {
	if strings.TrimSpace(cfg.PythonExecutable) == "" {
		return nil, fmt.Errorf("python executable is empty")
	}
	if strings.TrimSpace(cfg.DetectorScript) == "" {
		return nil, fmt.Errorf("detector script is empty")
	}
	if _, err := os.Stat(cfg.DetectorScript); err != nil {
		return nil, fmt.Errorf("detector script %q not found: %w", cfg.DetectorScript, err)
	}

	cmd := exec.Command(cfg.PythonExecutable, cfg.DetectorScript)
	cmd.Env = append(os.Environ(),
		"YOLO_MODEL="+cfg.YOLOModel,
		fmt.Sprintf("CONFIDENCE_THRESHOLD=%.6f", cfg.ConfidenceThreshold),
		fmt.Sprintf("INFERENCE_SCALE=%.6f", cfg.InferenceScale),
		fmt.Sprintf("YOLO_IMGSZ=%d", cfg.YOLOImgSz),
	)

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("detector stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("detector stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("detector stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start detector worker: %w", err)
	}

	go func() {
		s := bufio.NewScanner(stderr)
		for s.Scan() {
			line := strings.TrimSpace(s.Text())
			if line != "" {
				log.Printf("[detector] %s", line)
			}
		}
	}()

	return &Worker{
		cmd:    cmd,
		stdin:  stdin,
		stdout: bufio.NewReader(stdout),
	}, nil
}

// DetectJPEG pošle snímek workeru a vrátí detekce.
// Volání je serializované mutexem, protože worker běží sekvenčně nad jedním stdin/stdout.
func (w *Worker) DetectJPEG(jpg []byte) ([]model.Detection, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	if w == nil || w.cmd == nil || w.stdin == nil || w.stdout == nil {
		return nil, fmt.Errorf("detector worker not ready")
	}

	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(jpg)))
	if _, err := w.stdin.Write(header[:]); err != nil {
		return nil, fmt.Errorf("write detector header: %w", err)
	}
	if _, err := w.stdin.Write(jpg); err != nil {
		return nil, fmt.Errorf("write detector payload: %w", err)
	}

	line, err := w.stdout.ReadBytes('\n')
	if err != nil {
		return nil, fmt.Errorf("read detector response: %w", err)
	}

	var resp workerResponse
	if err := json.Unmarshal(bytes.TrimSpace(line), &resp); err != nil {
		return nil, fmt.Errorf("decode detector response: %w", err)
	}
	if resp.Error != "" {
		return nil, fmt.Errorf(resp.Error)
	}
	return resp.Detections, nil
}

// Close ukončí worker proces a uvolní související prostředky.
func (w *Worker) Close() {
	if w == nil || w.cmd == nil {
		return
	}
	if w.stdin != nil {
		_ = w.stdin.Close()
	}
	if w.cmd.Process != nil {
		_ = w.cmd.Process.Kill()
	}
	_ = w.cmd.Wait()
}

// FakeDetections vytvoří deterministické syntetické detekce pro vývoj a testy.
// Umožňuje běh pipeline bez reálného modelu/inferenčního backendu.
func FakeDetections(cfg config.Config, frameID int64, frameW, frameH int) []model.Detection {
	if !cfg.EnableDetection {
		return []model.Detection{}
	}
	if frameW <= 0 || frameH <= 0 {
		return []model.Detection{}
	}

	if frameID%12 < 3 {
		return []model.Detection{}
	}

	boxW := util.MaxInt(80, frameW/7)
	boxH := util.MaxInt(120, frameH/3)
	maxX := util.MaxInt(1, frameW-boxW-1)
	maxY := util.MaxInt(1, frameH-boxH-1)

	x := int(20 + (frameID*7)%int64(maxX))
	y := int(20 + (frameID*3)%int64(maxY))

	conf := cfg.ConfidenceThreshold + 0.15
	if conf > 0.99 {
		conf = 0.99
	}

	return []model.Detection{{
		X1:         x,
		Y1:         y,
		X2:         x + boxW,
		Y2:         y + boxH,
		Confidence: math.Round(conf*1000) / 1000,
	}}
}
