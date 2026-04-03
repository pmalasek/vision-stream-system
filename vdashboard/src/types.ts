export interface Detection {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  confidence: number;
}

export interface DetectionEvent {
  frame_id: number;
  timestamp: string;
  person_count: number;
  detections: Detection[];
}

export interface StatsEvent {
  fps: number;
  total_frames: number;
  total_detections: number;
  uptime_seconds: number;
  status:
    | "idle"
    | "starting"
    | "connecting"
    | "streaming"
    | "reconnecting"
    | "stopped"
    | "error";
}
