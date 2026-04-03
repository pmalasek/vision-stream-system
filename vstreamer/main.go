// Package main implements a RTSP relay server that serves a video file as an RTSP stream,
// behaving like an IP camera. It uses gortsplib v4 as the RTSP server and spawns ffmpeg
// as a subprocess to decode/re-encode and publish the video.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"
	"time"

	gortsplib "github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/pion/rtp"
)

// serverHandler implements the gortsplib v4 server handler interface.
// It acts as a relay: ffmpeg publishes to it, and readers (e.g. OpenCV/vprocessor) consume from it.
type serverHandler struct {
	server    *gortsplib.Server
	mutex     sync.RWMutex
	stream    *gortsplib.ServerStream
	publisher *gortsplib.ServerSession

	// write-error rate limiting – keeps the log readable when a slow consumer
	// (e.g. vprocessor blocked on YOLO inference) can't drain the queue fast
	// enough.  OnStreamWriteError is called by gortsplib for every dropped
	// packet; we collapse the bursts into a single summary line.
	writeErrMu      sync.Mutex
	writeErrLastLog time.Time
	writeErrDropped int
}

// OnConnOpen is called when a new TCP connection is opened.
func (sh *serverHandler) OnConnOpen(ctx *gortsplib.ServerHandlerOnConnOpenCtx) {
	log.Printf("[INFO] connection opened from %v", ctx.Conn.NetConn().RemoteAddr())
}

// OnConnClose is called when a TCP connection is closed.
func (sh *serverHandler) OnConnClose(ctx *gortsplib.ServerHandlerOnConnCloseCtx) {
	log.Printf("[INFO] connection closed (%v)", ctx.Error)
}

// OnSessionOpen is called when a new RTSP session is opened.
func (sh *serverHandler) OnSessionOpen(ctx *gortsplib.ServerHandlerOnSessionOpenCtx) {
	log.Printf("[INFO] session opened")
}

// OnSessionClose is called when an RTSP session is closed.
// If the closing session is the publisher (ffmpeg), tear down the stream.
func (sh *serverHandler) OnSessionClose(ctx *gortsplib.ServerHandlerOnSessionCloseCtx) {
	log.Printf("[INFO] session closed")

	sh.mutex.Lock()
	defer sh.mutex.Unlock()

	if sh.stream != nil && ctx.Session == sh.publisher {
		log.Printf("[INFO] publisher disconnected, closing stream")
		sh.stream.Close()
		sh.stream = nil
		sh.publisher = nil
	}
}

// OnDescribe is called when a client sends a DESCRIBE request (wants to read the stream).
func (sh *serverHandler) OnDescribe(
	ctx *gortsplib.ServerHandlerOnDescribeCtx,
) (*base.Response, *gortsplib.ServerStream, error) {
	log.Printf("[INFO] DESCRIBE request from %v (path: %q)", ctx.Conn.NetConn().RemoteAddr(), ctx.Path)

	sh.mutex.RLock()
	defer sh.mutex.RUnlock()

	// No publisher yet — stream not available
	if sh.stream == nil {
		log.Printf("[WARN] DESCRIBE received but no stream is available yet")
		return &base.Response{
			StatusCode: base.StatusNotFound,
		}, nil, nil
	}

	return &base.Response{
		StatusCode: base.StatusOK,
	}, sh.stream, nil
}

// OnAnnounce is called when ffmpeg sends an ANNOUNCE request (wants to publish).
// We create a new ServerStream from the announced description.
func (sh *serverHandler) OnAnnounce(
	ctx *gortsplib.ServerHandlerOnAnnounceCtx,
) (*base.Response, error) {
	log.Printf("[INFO] ANNOUNCE request from %v (path: %q)", ctx.Conn.NetConn().RemoteAddr(), ctx.Path)

	sh.mutex.Lock()
	defer sh.mutex.Unlock()

	// If a stream already exists, close it before replacing
	if sh.stream != nil {
		log.Printf("[INFO] replacing existing stream from previous publisher")
		sh.stream.Close()
		if sh.publisher != nil {
			sh.publisher.Close()
		}
	}

	// Create a new server stream from the announced session description
	sh.stream = gortsplib.NewServerStream(sh.server, ctx.Description)
	sh.publisher = ctx.Session

	log.Printf("[INFO] publisher registered, stream created with %d media(s)",
		len(ctx.Description.Medias))

	return &base.Response{
		StatusCode: base.StatusOK,
	}, nil
}

// OnSetup is called when a SETUP request is received.
// For publishers (ffmpeg in record mode), return OK with no stream.
// For readers, return the current stream.
func (sh *serverHandler) OnSetup(
	ctx *gortsplib.ServerHandlerOnSetupCtx,
) (*base.Response, *gortsplib.ServerStream, error) {
	log.Printf("[INFO] SETUP request")

	// Publisher (ffmpeg recording) — no stream to return at setup time
	if ctx.Session.State() == gortsplib.ServerSessionStatePreRecord {
		return &base.Response{
			StatusCode: base.StatusOK,
		}, nil, nil
	}

	sh.mutex.RLock()
	defer sh.mutex.RUnlock()

	if sh.stream == nil {
		return &base.Response{
			StatusCode: base.StatusNotFound,
		}, nil, nil
	}

	return &base.Response{
		StatusCode: base.StatusOK,
	}, sh.stream, nil
}

// OnPlay is called when a reader sends a PLAY request.
func (sh *serverHandler) OnPlay(
	ctx *gortsplib.ServerHandlerOnPlayCtx,
) (*base.Response, error) {
	log.Printf("[INFO] PLAY request — reader starting playback")

	return &base.Response{
		StatusCode: base.StatusOK,
	}, nil
}

// OnRecord is called when ffmpeg sends a RECORD request (starts streaming data).
// We register the per-packet callback here to relay all RTP packets to readers.
func (sh *serverHandler) OnRecord(
	ctx *gortsplib.ServerHandlerOnRecordCtx,
) (*base.Response, error) {
	log.Printf("[INFO] RECORD request — ffmpeg is now publishing RTP packets")

	// Register a callback for every RTP packet received from the publisher.
	// OnPacketRTPAny fires for packets on any media track.
	ctx.Session.OnPacketRTPAny(func(medi *description.Media, _ format.Format, pkt *rtp.Packet) {
		sh.mutex.RLock()
		stream := sh.stream
		sh.mutex.RUnlock()

		if stream == nil {
			return
		}

		// Errors here (e.g. "write queue is full") are reported to
		// OnStreamWriteError below; no need to log them twice.
		_ = stream.WritePacketRTP(medi, pkt)
	})

	return &base.Response{
		StatusCode: base.StatusOK,
	}, nil
}

// OnStreamWriteError is called by gortsplib whenever it cannot deliver a
// packet to a reader session (most commonly because the reader's outgoing
// write queue is full, i.e. the consumer is slower than the producer).
//
// Root cause: vprocessor blocks cap.read() while running YOLO inference,
// so it consumes frames at the inference rate (~10 fps) while the RTSP
// source produces them at the capture rate (~30 fps).  The per-reader
// write queue fills up and gortsplib drops the excess packets.
//
// To avoid log spam we rate-limit the message to at most one line every
// 5 seconds, summarising how many packets were dropped in that window.
func (sh *serverHandler) OnStreamWriteError(ctx *gortsplib.ServerHandlerOnStreamWriteErrorCtx) {
	sh.writeErrMu.Lock()
	sh.writeErrDropped++
	count := sh.writeErrDropped
	lastLog := sh.writeErrLastLog
	sh.writeErrMu.Unlock()

	if time.Since(lastLog) >= 5*time.Second {
		sh.writeErrMu.Lock()
		sh.writeErrLastLog = time.Now()
		sh.writeErrDropped = 0
		sh.writeErrMu.Unlock()

		log.Printf("[WARN] slow RTSP reader – write queue full, %d packet(s) dropped "+
			"in the last 5 s. The consumer (vprocessor) is reading slower than the "+
			"stream is produced. Consider a larger WriteQueueSize or decoupling "+
			"RTSP reading from inference in vprocessor.", count)
	}
}

// buildFFmpegArgs constructs the ffmpeg command-line arguments.
func buildFFmpegArgs(videoFile string, port int, streamPath string, loop bool) []string {
	args := []string{"-re"}

	if loop {
		args = append(args, "-stream_loop", "-1")
	}

	args = append(args,
		"-i", videoFile,
		"-c:v", "libx264",
		"-preset", "ultrafast",
		"-tune", "zerolatency",
		"-pix_fmt", "yuv420p",
		"-an", // no audio
		"-rtsp_transport", "tcp",
		"-f", "rtsp",
		fmt.Sprintf("rtsp://127.0.0.1:%d/%s", port, streamPath),
	)

	return args
}

// runFFmpegLoop spawns ffmpeg and, if loop is enabled, restarts it when it exits.
// It returns when ctx is cancelled.
func runFFmpegLoop(ctx context.Context, videoFile string, port int, streamPath string, loop bool) {
	for {
		// Check for shutdown before each attempt
		select {
		case <-ctx.Done():
			log.Printf("[INFO] ffmpeg loop stopping (context cancelled)")
			return
		default:
		}

		args := buildFFmpegArgs(videoFile, port, streamPath, loop)
		log.Printf("[INFO] spawning ffmpeg with args: %v", args)

		cmd := exec.CommandContext(ctx, "ffmpeg", args...)
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr

		if err := cmd.Run(); err != nil {
			select {
			case <-ctx.Done():
				log.Printf("[INFO] ffmpeg stopped (shutdown requested)")
				return
			default:
				log.Printf("[WARN] ffmpeg exited with error: %v", err)
			}
		} else {
			log.Printf("[INFO] ffmpeg exited cleanly")
		}

		// If loop is disabled, stop after the first run
		if !loop {
			log.Printf("[INFO] loop disabled — not restarting ffmpeg")
			return
		}

		log.Printf("[INFO] restarting ffmpeg in 1 second...")
		select {
		case <-ctx.Done():
			return
		case <-time.After(1 * time.Second):
		}
	}
}

func main() {
	// ── CLI flags ──────────────────────────────────────────────────────────────
	videoFile := flag.String("video", "", "path to video file (required)")
	port := flag.Int("port", 8554, "RTSP server port")
	udpRTPPort := flag.Int("udp-rtp", 8000, "UDP port for RTP packets (0 = disable UDP)")
	udpRTCPPort := flag.Int("udp-rtcp", 8001, "UDP port for RTCP packets (0 = disable UDP)")
	streamPath := flag.String("path", "live", "RTSP stream path (e.g. 'live' → rtsp://host:port/live)")
	loop := flag.Bool("loop", true, "loop the video when it ends")
	flag.Parse()

	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	// Validate required flags
	if *videoFile == "" {
		fmt.Fprintln(os.Stderr, "ERROR: --video flag is required")
		fmt.Fprintln(os.Stderr, "Usage:")
		flag.PrintDefaults()
		os.Exit(1)
	}

	if _, err := os.Stat(*videoFile); os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "ERROR: video file not found: %s\n", *videoFile)
		os.Exit(1)
	}

	log.Printf("[INFO] starting vstreamer")
	log.Printf("[INFO]   video file  : %s", *videoFile)
	log.Printf("[INFO]   RTSP port   : %d", *port)
	log.Printf("[INFO]   UDP RTP port: %d", *udpRTPPort)
	log.Printf("[INFO]   UDP RTCP port: %d", *udpRTCPPort)
	log.Printf("[INFO]   stream path : %s", *streamPath)
	log.Printf("[INFO]   loop        : %v", *loop)
	log.Printf("[INFO] VLC tip: vlc --no-satip-enable rtsp://localhost:%d/%s", *port, *streamPath)

	// ── RTSP server ────────────────────────────────────────────────────────────
	h := &serverHandler{}

	srv := &gortsplib.Server{
		Handler:     h,
		RTSPAddress: fmt.Sprintf(":%d", *port),
		// Increase the per-reader outgoing packet queue beyond the default
		// of 256 so that brief bursts of slow consumption (e.g. during a
		// heavy YOLO inference frame) don't immediately overflow.  At
		// ~30 fps with typical H.264 fragmentation (~5 RTP packets/frame)
		// this gives roughly 6 seconds of headroom instead of ~1.7 s.
		WriteQueueSize: 1024,
	}

	// Enable UDP transport so clients like VLC can connect without --rtsp-tcp.
	// Both ports must be non-zero to activate UDP.
	if *udpRTPPort > 0 && *udpRTCPPort > 0 {
		srv.UDPRTPAddress = fmt.Sprintf(":%d", *udpRTPPort)
		srv.UDPRTCPAddress = fmt.Sprintf(":%d", *udpRTCPPort)
		log.Printf("[INFO] UDP transport enabled (RTP :%d, RTCP :%d)", *udpRTPPort, *udpRTCPPort)
	} else {
		log.Printf("[INFO] UDP transport disabled — clients must use TCP (e.g. vlc --rtsp-tcp …)")
	}

	h.server = srv

	if err := h.server.Start(); err != nil {
		log.Fatalf("[ERROR] failed to start RTSP server: %v", err)
	}
	log.Printf("[INFO] RTSP server listening on :%d", *port)
	log.Printf("[INFO] readers can connect to: rtsp://localhost:%d/%s", *port, *streamPath)

	// ── Shutdown context ───────────────────────────────────────────────────────
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Catch SIGINT / SIGTERM for graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigChan
		log.Printf("[INFO] received signal %v — initiating graceful shutdown", sig)
		cancel()
	}()

	// ── ffmpeg subprocess ──────────────────────────────────────────────────────
	// Give the server a short moment to be fully ready before ffmpeg connects
	log.Printf("[INFO] waiting 500ms for server to be ready before spawning ffmpeg...")
	select {
	case <-ctx.Done():
		log.Printf("[INFO] context cancelled before ffmpeg could start")
		h.server.Close()
		return
	case <-time.After(500 * time.Millisecond):
	}

	go runFFmpegLoop(ctx, *videoFile, *port, *streamPath, *loop)

	// ── Wait for shutdown ──────────────────────────────────────────────────────
	<-ctx.Done()
	log.Printf("[INFO] shutting down RTSP server...")
	h.server.Close()
	log.Printf("[INFO] vstreamer stopped")
}
