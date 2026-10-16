FROM node:22-alpine AS web
WORKDIR /w
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM golang:1.22-alpine AS probe
WORKDIR /p
COPY probe ./
RUN for a in amd64 arm64; do CGO_ENABLED=0 GOOS=linux GOARCH=$a go build -trimpath -ldflags="-s -w" -o /out/hbm-probe-linux-$a . ; done

FROM python:3.12-slim
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=probe /out ./probes
COPY --from=web /w/dist ./static
ENV DB_PATH=/data/hbm.db PROBE_DIR=/app/probes STATIC_DIR=/app/static FORWARDED_ALLOW_IPS=*
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["uvicorn","--factory","app.main:create_app","--host","0.0.0.0","--port","8000","--proxy-headers"]
