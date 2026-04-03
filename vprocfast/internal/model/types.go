package model

// Detection reprezentuje jeden bounding box detekované osoby
// v pixelových souřadnicích snímku.
type Detection struct {
	X1         int     `json:"x1"`
	Y1         int     `json:"y1"`
	X2         int     `json:"x2"`
	Y2         int     `json:"y2"`
	Confidence float64 `json:"confidence"`
}

// DetectionEvent je payload posílaný klientům pro konkrétní snímek.
type DetectionEvent struct {
	FrameID     int64       `json:"frame_id"`
	Timestamp   float64     `json:"timestamp"`
	PersonCount int         `json:"person_count"`
	Detections  []Detection `json:"detections"`
}

// StatsEvent reprezentuje agregované runtime metriky backendu.
type StatsEvent struct {
	FPS             float64 `json:"fps"`
	TotalFrames     int64   `json:"total_frames"`
	TotalDetections int64   `json:"total_detections"`
	UptimeSeconds   float64 `json:"uptime_seconds"`
	Status          string  `json:"status"`
}
