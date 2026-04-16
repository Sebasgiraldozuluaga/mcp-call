FROM python:3.12-slim

# Node.js para npx (MCP postgres server)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-caché del paquete MCP para no descargarlo en runtime
RUN npx --yes @modelcontextprotocol/server-postgres --help 2>/dev/null || true

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
