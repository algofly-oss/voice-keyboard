# Whisper server plus the streaming gateway in one container.
# The gateway reuses the image's own virtualenv (FastAPI, uvicorn, httpx, numpy).
FROM fedirz/faster-whisper-server:latest-cuda
COPY backend/gateway/app.py backend/gateway/accounts.py backend/gateway/hinglish.py backend/gateway/start.sh /opt/gateway/
COPY backend/gateway/desktop /opt/gateway/desktop
COPY web /opt/web
# Decoding options for the bundled Whisper server (WHISPER_BEAM_SIZE).
COPY backend/gateway/vk_whisper_tuning.py /root/faster-whisper-server/.venv/lib/python3.12/site-packages/
RUN echo 'import vk_whisper_tuning' > /root/faster-whisper-server/.venv/lib/python3.12/site-packages/vk_whisper_tuning.pth
ENV WHISPER_URL=http://127.0.0.1:8001
EXPOSE 8000
CMD ["/opt/gateway/start.sh"]
