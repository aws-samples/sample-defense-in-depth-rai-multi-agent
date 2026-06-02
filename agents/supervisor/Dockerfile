FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1

COPY requirements.txt requirements.txt

# Combined RUN: install deps + otel + create user (no cache)
RUN uv pip install --no-cache-dir -r requirements.txt && \
    uv pip install --no-cache-dir "aws-opentelemetry-distro==0.10.1" && \
    useradd -m -u 1000 bedrock_agentcore

USER bedrock_agentcore

EXPOSE 8080 8000

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost',8080)); s.close()" || exit 1

CMD ["opentelemetry-instrument", "python", "-m", "agent"]
