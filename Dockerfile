FROM python:3.11-slim

# Install git for any tree-sitter or fetching dependencies
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all python scripts and LanceDB vector databases
COPY . .

# Ensure the mcp server binds to stdio properly
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "server.py"]
