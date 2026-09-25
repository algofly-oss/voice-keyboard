#!/bin/bash
# Runs Whisper on localhost only and the gateway on the public port.
# The container stops when either process exits.
trap 'kill 0' TERM INT
cd /root/faster-whisper-server
.venv/bin/uvicorn --factory faster_whisper_server.main:create_app --host 0.0.0.0 --port "${TRANSCRIPTION_PORT:-8001}" &
.venv/bin/uvicorn --app-dir /opt/gateway app:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips '*' &
wait -n
status=$?
kill 0
exit $status
