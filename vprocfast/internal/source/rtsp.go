package source

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log"
	"os/exec"
	"strings"
)

type RTSPConsumerOptions struct {
	FFmpegPath    string
	RTSPTransport string
	RTSPURL       string
	FPS           int
	OnStarted     func()
}

// ConsumeRTSP připojí ffmpeg k RTSP streamu a předává JPEG snímky callbacku.
//
// Funkce je blokující a běží do zrušení contextu, EOF nebo chyby.
// Přenos snímků je přes image2pipe (mjpeg) kvůli jednoduché integraci v Go.
func ConsumeRTSP(ctx context.Context, opts RTSPConsumerOptions, onFrame func([]byte) error) error {
	if onFrame == nil {
		return fmt.Errorf("onFrame callback is required")
	}

	args := []string{
		"-hide_banner",
		"-loglevel", "error",
		"-rtsp_transport", opts.RTSPTransport,
		"-i", opts.RTSPURL,
		"-an",
		"-vf", fmt.Sprintf("fps=%d", opts.FPS),
		"-f", "image2pipe",
		"-vcodec", "mjpeg",
		"-q:v", "5",
		"-",
	}

	cmd := exec.CommandContext(ctx, opts.FFmpegPath, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return fmt.Errorf("stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start ffmpeg: %w", err)
	}

	go func() {
		b, _ := io.ReadAll(stderr)
		if len(b) > 0 && ctx.Err() == nil {
			msg := strings.TrimSpace(string(b))
			if len(msg) > 500 {
				msg = msg[len(msg)-500:]
			}
			log.Printf("[ffmpeg] %s", msg)
		}
	}()

	reader := bufio.NewReaderSize(stdout, 1<<20)
	if opts.OnStarted != nil {
		opts.OnStarted()
	}

	frameCh := make(chan []byte, 1)
	readerDone := make(chan error, 1)

	go func() {
		for {
			jpg, err := readNextJPEG(reader)
			if err != nil {
				readerDone <- err
				return
			}

			select {
			case frameCh <- jpg:
			default:
				select {
				case <-frameCh:
				default:
				}
				select {
				case frameCh <- jpg:
				default:
				}
			}
		}
	}()

	for {
		select {
		case <-ctx.Done():
			_ = cmd.Wait()
			return ctx.Err()
		case err := <-readerDone:
			_ = cmd.Wait()
			if err == io.EOF {
				return io.EOF
			}
			return err
		case jpg := <-frameCh:
			if err := onFrame(jpg); err != nil {
				_ = cmd.Wait()
				return err
			}
		}
	}
}

// readNextJPEG čte z byte streamu další kompletní JPEG (SOI..EOI).
// Je robustní vůči arbitrárnímu dělení chunků na stdin/stdout pipe.
func readNextJPEG(r *bufio.Reader) ([]byte, error) {
	for {
		b, err := r.ReadByte()
		if err != nil {
			return nil, err
		}
		if b != 0xFF {
			continue
		}

		b2, err := r.ReadByte()
		if err != nil {
			return nil, err
		}
		if b2 != 0xD8 {
			continue
		}

		buf := []byte{0xFF, 0xD8}
		prev := byte(0xD8)

		for {
			next, err := r.ReadByte()
			if err != nil {
				return nil, err
			}
			buf = append(buf, next)
			if prev == 0xFF && next == 0xD9 {
				return buf, nil
			}
			prev = next
		}
	}
}
