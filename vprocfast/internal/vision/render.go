package vision

import (
	"bytes"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"

	"vprocfast/internal/model"
)

// AnnotateJPEG dekóduje JPEG, nechá zavolat detekci a vykreslí boxy do výstupu.
// Vrací anotovaný JPEG a seznam detekcí použitých pro vykreslení.
func AnnotateJPEG(
	jpg []byte,
	quality int,
	detectFn func(frameW, frameH int) []model.Detection,
) ([]byte, []model.Detection) {
	imgSrc, _, err := image.Decode(bytes.NewReader(jpg))
	if err != nil {
		return jpg, []model.Detection{}
	}

	rgba := toRGBA(imgSrc)
	bounds := rgba.Bounds()
	detections := detectFn(bounds.Dx(), bounds.Dy())

	for _, d := range detections {
		drawBox(rgba, d.X1, d.Y1, d.X2, d.Y2, color.RGBA{R: 80, G: 255, B: 120, A: 255})
	}

	out := bytes.NewBuffer(nil)
	if err := jpeg.Encode(out, rgba, &jpeg.Options{Quality: quality}); err != nil {
		return jpg, detections
	}
	return out.Bytes(), detections
}

// BuildSyntheticFrameJPEG vytvoří syntetický testovací snímek včetně boxů.
// Slouží pro režim SOURCE_MODE=synthetic.
func BuildSyntheticFrameJPEG(frameID int64, evt model.DetectionEvent, quality int) []byte {
	const (
		width  = 1280
		height = 720
	)

	img := image.NewRGBA(image.Rect(0, 0, width, height))
	draw.Draw(img, img.Bounds(), &image.Uniform{C: color.RGBA{R: 18, G: 24, B: 38, A: 255}}, image.Point{}, draw.Src)

	barW := int((frameID * 13) % width)
	draw.Draw(
		img,
		image.Rect(0, 0, barW, 10),
		&image.Uniform{C: color.RGBA{R: 255, G: 80, B: 80, A: 255}},
		image.Point{},
		draw.Src,
	)

	for _, d := range evt.Detections {
		drawBox(img, d.X1, d.Y1, d.X2, d.Y2, color.RGBA{R: 80, G: 255, B: 120, A: 255})
	}

	buf := bytes.NewBuffer(nil)
	_ = jpeg.Encode(buf, img, &jpeg.Options{Quality: quality})
	return buf.Bytes()
}

// toRGBA převede obecný image.Image na mutable RGBA buffer.
func toRGBA(src image.Image) *image.RGBA {
	bounds := src.Bounds()
	dst := image.NewRGBA(bounds)
	draw.Draw(dst, bounds, src, bounds.Min, draw.Src)
	return dst
}

// drawBox vykreslí obrys obdélníku o tloušťce 2 px.
func drawBox(img *image.RGBA, x1, y1, x2, y2 int, c color.Color) {
	if x1 < 0 {
		x1 = 0
	}
	if y1 < 0 {
		y1 = 0
	}
	if x2 > img.Bounds().Dx() {
		x2 = img.Bounds().Dx()
	}
	if y2 > img.Bounds().Dy() {
		y2 = img.Bounds().Dy()
	}
	if x2 <= x1 || y2 <= y1 {
		return
	}

	for x := x1; x < x2; x++ {
		img.Set(x, y1, c)
		if y1+1 < img.Bounds().Dy() {
			img.Set(x, y1+1, c)
		}
		if y2-1 >= 0 {
			img.Set(x, y2-1, c)
		}
		if y2-2 >= 0 {
			img.Set(x, y2-2, c)
		}
	}
	for y := y1; y < y2; y++ {
		img.Set(x1, y, c)
		if x1+1 < img.Bounds().Dx() {
			img.Set(x1+1, y, c)
		}
		if x2-1 >= 0 {
			img.Set(x2-1, y, c)
		}
		if x2-2 >= 0 {
			img.Set(x2-2, y, c)
		}
	}
}
