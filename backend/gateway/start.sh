#!/bin/bash
# Runs Whisper on localhost only and the gateway on the public port.
# The container stops when either process exits.
trap 'kill 0' TERM INT

# WHISPER_VARIANT picks the model (set it in .env):
#   transcribe  large-v3-turbo; types what you say, in the language you say it
#   translate   large-v3; types an English translation of any spoken language
#               (turbo was trained without the translate task)
case "${WHISPER_VARIANT:=transcribe}" in
  transcribe) default_model=deepdml/faster-whisper-large-v3-turbo-ct2 ;;
  translate) default_model=Systran/faster-whisper-large-v3 ;;
  *) echo "WHISPER_VARIANT must be transcribe or translate, not '$WHISPER_VARIANT'" >&2; exit 1 ;;
esac
export WHISPER_VARIANT
export WHISPER_MODEL="${WHISPER_MODEL:-$default_model}"   # an explicit WHISPER_MODEL still wins
export WHISPER__MODEL="$WHISPER_MODEL"                     # the model the Whisper server preloads
# WHISPER_DEVICE comes from the compose profile (gpu -> cuda, cpu -> cpu).
case "${WHISPER_DEVICE:=cuda}" in
  cuda) default_compute=int8_float16 ;;
  cpu) default_compute=int8 ;;   # float16 kernels need a GPU
  *) echo "WHISPER_DEVICE must be cuda or cpu, not '$WHISPER_DEVICE'" >&2; exit 1 ;;
esac
export WHISPER__INFERENCE_DEVICE="$WHISPER_DEVICE"
export WHISPER__COMPUTE_TYPE="${WHISPER_COMPUTE_TYPE:-$default_compute}"
export WHISPER__CPU_THREADS="${WHISPER_CPU_THREADS:-$(nproc)}"
echo "Whisper: $WHISPER_VARIANT with $WHISPER_MODEL on $WHISPER_DEVICE ($WHISPER__COMPUTE_TYPE)"
cd /root/faster-whisper-server
.venv/bin/uvicorn --factory faster_whisper_server.main:create_app --host 0.0.0.0 --port "${TRANSCRIPTION_PORT:-8001}" &
.venv/bin/uvicorn --app-dir /opt/gateway app:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips '*' &
wait -n
status=$?
kill 0
exit $status
