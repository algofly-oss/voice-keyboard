# Whisper server plus the streaming gateway in one container.
# The gateway reuses the image's own virtualenv (FastAPI, uvicorn, httpx, numpy).
FROM fedirz/faster-whisper-server:latest-cuda
COPY gateway/app.py gateway/start.sh /opt/gateway/
ENV WHISPER_URL=http://127.0.0.1:8001
EXPOSE 8000
CMD ["/opt/gateway/start.sh"]
