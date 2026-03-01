# Base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy repo
COPY . /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    libssl-dev \
    build-essential \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt
RUN pip install imdbpy

# Create logs directory
RUN mkdir -p /app/logs

# Expose port
EXPOSE 8000

# Start supervisord
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
