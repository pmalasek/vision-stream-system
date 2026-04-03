FROM node:20-alpine AS builder
WORKDIR /app
ARG VITE_PROCESSOR_URL
ARG VITE_STREAM_URL
ARG VITE_WEBRTC_URL
ARG VITE_USE_WEBRTC
ENV VITE_PROCESSOR_URL=$VITE_PROCESSOR_URL
ENV VITE_STREAM_URL=$VITE_STREAM_URL
ENV VITE_WEBRTC_URL=$VITE_WEBRTC_URL
ENV VITE_USE_WEBRTC=$VITE_USE_WEBRTC
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
