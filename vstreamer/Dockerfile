FROM golang:1.21-alpine AS builder

RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN go build -o vstreamer .

FROM alpine:latest

RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY --from=builder /app/vstreamer .

ENTRYPOINT ["./vstreamer"]
