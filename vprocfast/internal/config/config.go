package config

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

type Config struct {
	Host                string
	Port                int
	OutputDir           string
	FPS                 int
	ConfidenceThreshold float64
	EnableDetection     bool
	DetectionBackend    string
	DetectEveryN        int
	SourceMode          string
	RTSPURL             string
	RTSPTransport       string
	FFmpegPath          string
	JPEGQuality         int
	PythonExecutable    string
	DetectorScript      string
	YOLOModel           string
	InferenceScale      float64
	YOLOImgSz           int
}

func getenv(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv("VPROCFAST_" + key)); v != "" {
		return v
	}
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}

func getenvInt(key string, fallback int) int {
	v := strings.TrimSpace(os.Getenv("VPROCFAST_" + key))
	if v == "" {
		v = strings.TrimSpace(os.Getenv(key))
	}
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func getenvFloat64(key string, fallback float64) float64 {
	v := strings.TrimSpace(os.Getenv("VPROCFAST_" + key))
	if v == "" {
		v = strings.TrimSpace(os.Getenv(key))
	}
	if v == "" {
		return fallback
	}
	n, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return fallback
	}
	return n
}

func getenvBool(key string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv("VPROCFAST_" + key)))
	if v == "" {
		v = strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	}
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func moduleDir() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		return "."
	}
	return filepath.Dir(file)
}

func firstExistingPath(paths ...string) string {
	for _, p := range paths {
		if strings.TrimSpace(p) == "" {
			continue
		}
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

func defaultPythonExecutable() string {
	base := moduleDir()
	candidates := []string{
		filepath.Join(base, "..", "..", "..", "vprocessor", ".venv", "bin", "python"),
		filepath.Join(base, "..", "..", ".venv", "bin", "python"),
	}
	if p := firstExistingPath(candidates...); p != "" {
		return p
	}
	if p, err := exec.LookPath("python3"); err == nil {
		return p
	}
	if p, err := exec.LookPath("python"); err == nil {
		return p
	}
	return "python3"
}

func defaultDetectorScript() string {
	base := moduleDir()
	if p := firstExistingPath(filepath.Join(base, "..", "..", "detect_worker.py")); p != "" {
		return p
	}
	return filepath.Join(base, "..", "..", "detect_worker.py")
}

func Load() Config {
	fps := getenvInt("FPS", 10)
	if fps <= 0 {
		fps = 10
	}

	port := getenvInt("PORT", 8001)
	if port <= 0 {
		port = 8001
	}

	jpegQuality := getenvInt("JPEG_QUALITY", 80)
	if jpegQuality < 30 {
		jpegQuality = 30
	}
	if jpegQuality > 95 {
		jpegQuality = 95
	}

	detectEveryN := getenvInt("DETECT_EVERY_N", 1)
	if detectEveryN <= 0 {
		detectEveryN = 1
	}

	sourceMode := strings.ToLower(getenv("SOURCE_MODE", "synthetic"))
	if sourceMode != "synthetic" && sourceMode != "rtsp" {
		sourceMode = "synthetic"
	}

	detectionBackend := strings.ToLower(getenv("DETECTION_BACKEND", ""))
	if detectionBackend == "" {
		if sourceMode == "rtsp" && getenvBool("ENABLE_DETECTION", true) {
			detectionBackend = "python"
		} else {
			detectionBackend = "fake"
		}
	}
	if detectionBackend != "fake" && detectionBackend != "python" {
		detectionBackend = "fake"
	}

	transport := strings.ToLower(getenv("RTSP_TRANSPORT", "tcp"))
	if transport != "tcp" && transport != "udp" {
		transport = "tcp"
	}

	inferenceScale := getenvFloat64("INFERENCE_SCALE", 1.0)
	if inferenceScale <= 0 || inferenceScale > 1.0 {
		inferenceScale = 1.0
	}

	yoloImgSz := getenvInt("YOLO_IMGSZ", 640)
	if yoloImgSz <= 0 {
		yoloImgSz = 640
	}

	return Config{
		Host:                getenv("HOST", "0.0.0.0"),
		Port:                port,
		OutputDir:           getenv("OUTPUT_DIR", "./output"),
		FPS:                 fps,
		ConfidenceThreshold: getenvFloat64("CONFIDENCE_THRESHOLD", 0.5),
		EnableDetection:     getenvBool("ENABLE_DETECTION", true),
		DetectionBackend:    detectionBackend,
		DetectEveryN:        detectEveryN,
		SourceMode:          sourceMode,
		RTSPURL:             getenv("RTSP_URL", "rtsp://vstreamer:8554/live"),
		RTSPTransport:       transport,
		FFmpegPath:          getenv("FFMPEG_PATH", "ffmpeg"),
		JPEGQuality:         jpegQuality,
		PythonExecutable:    getenv("PYTHON_EXECUTABLE", defaultPythonExecutable()),
		DetectorScript:      getenv("DETECTOR_SCRIPT", defaultDetectorScript()),
		YOLOModel:           getenv("YOLO_MODEL", "models/yolov8n.pt"),
		InferenceScale:      inferenceScale,
		YOLOImgSz:           yoloImgSz,
	}
}
