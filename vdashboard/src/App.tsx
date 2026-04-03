import Header from "./components/Header";
import VideoStream from "./components/VideoStream";
import StatsPanel from "./components/StatsPanel";
import DetectionPanel from "./components/DetectionPanel";
import { useSocket } from "./hooks/useSocket";

const PROCESSOR_BASE =
  (import.meta.env.VITE_PROCESSOR_URL as string | undefined) ??
  "http://localhost:8000";

const STREAM_URL = `${PROCESSOR_BASE}/stream`;

export default function App() {
  const { latestDetection, stats, isConnected } = useSocket();

  return (
    <div className="min-h-screen lg:h-screen bg-gray-900 text-white flex flex-col lg:overflow-hidden">
      <Header isConnected={isConnected} />

      <main className="flex-1 p-4 lg:p-6 lg:min-h-0 lg:overflow-hidden">
        <div className="max-w-screen-2xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-4 lg:gap-6 lg:h-full">
          {/* ── Left column: video stream (takes 2/3 on large screens) ──
               flex flex-col ensures h-full propagates into VideoStream     */}
          <div className="lg:col-span-2 flex flex-col lg:min-h-0">
            <VideoStream
              streamUrl={STREAM_URL}
              isConnected={isConnected}
              stats={stats}
            />
          </div>

          {/* ── Right column: stats + detections ──
               overflow-y-auto lets this column scroll on its own so the
               left column (and the video) never change height.            */}
          <div className="flex flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
            <StatsPanel stats={stats} isConnected={isConnected} />
            <DetectionPanel detection={latestDetection} />
          </div>
        </div>
      </main>

      <footer className="text-center text-gray-600 text-xs py-3 border-t border-gray-800 shrink-0">
        Vision Stream System &mdash; vdashboard
      </footer>
    </div>
  );
}
