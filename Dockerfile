FROM python:3.11-slim
LABEL maintainer="Document Processor Team"
LABEL description="Production-ready multi-lingual document processing system"

# Set working directory
WORKDIR /app

# Install system dependencies including Chromium dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-spa \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    tesseract-ocr-ara \
    tesseract-ocr-chi-sim \
    tesseract-ocr-hin \
    tesseract-ocr-rus \
    tesseract-ocr-jpn \
    tesseract-ocr-kor \
    libtesseract-dev \
    poppler-utils \
    curl \
    dos2unix \
    wget \
    # Chromium dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libatspi2.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxcb1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    libgtk-3-0 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies including Playwright
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser only (skip system deps as we installed them manually)
RUN python -m playwright install chromium

# Copy application code
COPY document_processor/ ./document_processor/

# Copy web UI
COPY web_ui/ ./web_ui/

# Fix line endings for all Python files (cross-platform compatibility)
RUN find . -type f -name "*.py" -exec dos2unix {} \;

# Create directories for data
RUN mkdir -p /data/documents

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose ports
EXPOSE 8000 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run application
CMD ["python", "-m", "document_processor.main"]