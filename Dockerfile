# Multi-stage build: Node.js + Python for YouTube Bot

# Stage 1: Node.js + Python base image
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Install Node.js and npm (required for yt-dlp challenge solving)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Verify installations
RUN node --version && npm --version && python --version

# Copy requirements first (for better Docker layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp

# Verify yt-dlp
RUN yt-dlp --version

# Copy application code
COPY app_production.py app.py
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 youtubebotuser && \
    chown -R youtubebotuser:youtubebotuser /app

USER youtubebotuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Expose port
EXPOSE 8000

# Start the application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]