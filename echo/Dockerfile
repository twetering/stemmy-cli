FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir -e .

COPY echo/ echo/

# /data  = database volume (stemmy-replica.db)
# /cache = audio cache volume (persistent tussen restarts)
VOLUME ["/data", "/cache"]

EXPOSE 7843

CMD ["python", "echo/server.py", "--db", "/data/stemmy-replica.db"]
