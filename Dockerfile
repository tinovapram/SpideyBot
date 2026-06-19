FROM denoland/deno:bin AS deno
FROM python:3.11-slim

# Copy Deno binary from official image stage
COPY --from=deno /deno /usr/local/bin/deno

# Install system dependencies including ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \ 
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure download directories are clean and exist
RUN mkdir -p data downloads

# Start the bot
CMD ["python", "main.py"]
