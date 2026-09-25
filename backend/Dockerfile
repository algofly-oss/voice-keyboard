# Whisper server plus the streaming gateway in one container.
# The gateway reuses the image's own virtualenv (FastAPI, uvicorn, httpx, numpy).
FROM fedirz/faster-whisper-server:latest-cuda
COPY backend/gateway/app.py backend/gateway/accounts.py backend/gateway/start.sh /opt/gateway/
COPY backend/gateway/desktop /opt/gateway/desktop
COPY web /opt/web
ENV WHISPER_URL=http://127.0.0.1:8001
EXPOSE 8000
CMD ["/opt/gateway/start.sh"]
