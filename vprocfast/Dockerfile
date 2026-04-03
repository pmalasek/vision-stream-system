FROM golang:1.21-bookworm AS builder

WORKDIR /app

COPY go.mod go.sum* ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o vprocfast .

FROM debian:bookworm-slim

RUN apt-get update \
	&& apt-get install -y --no-install-recommends ffmpeg ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/vprocfast /app/vprocfast

EXPOSE 8001
ENTRYPOINT ["/app/vprocfast"]
