# Plaud transcripts: hourly sync with local Whisper and a local LLM (CPU, portable engines).
FROM mambaorg/micromamba:2-debian12-slim

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.portable.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml && micromamba clean --all --yes

# Model cache lives in a volume; created here so the volume gets the right owner.
USER root
RUN mkdir -p /models /plaud /app/library && chown -R $MAMBA_USER:$MAMBA_USER /models /plaud /app
USER $MAMBA_USER

ENV HF_HOME=/models \
    PLAUD_LIBRARY=/app/library \
    PLAUD_TOKEN_FILE=/plaud/tokens.json \
    NO_OPEN=1 \
    NO_LOGIN=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PATH=/opt/conda/bin:$PATH

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER sync.py summarize.py diarize.py mcp_server.py loop.py viewer.html ./

CMD ["python", "loop.py"]
