# TargetDiscovery Agent - production container
# Build:  docker build -t target-discovery-agent:0.8.0 .
# Run:    docker run --rm -p 8888:8888 -v target-data:/data target-discovery-agent:0.8.0
FROM python:3.11-slim

ARG TARGET_EXTRAS=mcp
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RESEARCH_AGENT_PROJECT_DIR=/data/projects \
    TARGET_AGENT_RUN_DIR=/data/runs \
    TARGET_AGENT_CACHE_DIR=/data/cache \
    TARGET_AGENT_INPUT_ROOT=/data/input

WORKDIR /opt/target

COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
COPY workflows ./workflows
COPY skills ./skills
COPY paper_strategy ./paper_strategy

RUN pip install --upgrade pip && \
    pip install ".[${TARGET_EXTRAS}]" && \
    mkdir -p /data/projects /data/runs /data/cache /data/input && \
    useradd --create-home --uid 10001 --shell /usr/sbin/nologin target && \
    chown -R target:target /data

# Overridable persistent data volume (see docker-compose.yml)
VOLUME ["/data"]

EXPOSE 8888

USER target

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/healthz', timeout=4)" || exit 1

ENTRYPOINT ["target-agent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8888"]