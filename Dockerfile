FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg git curl procps && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY main.py .
COPY asi.py .

# Force port 10000 for Render health check
ENV PORT=10000

# Expose the port
EXPOSE 10000

# Run the bot
CMD ["python", "main.py"]
