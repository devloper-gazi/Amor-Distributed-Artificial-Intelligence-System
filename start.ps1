# Allow this script to run in the current PowerShell process
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# Amor Document Processor - Windows Startup Script
# This script starts the document processing system with all required services on Windows

# Requires PowerShell 5.1 or higher
#Requires -Version 5.1

# Set error action preference
$ErrorActionPreference = "Stop"

# Function to print colored output
function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Blue
}

function Write-Success {
    param([string]$Message)
    Write-Host "[SUCCESS] $Message" -ForegroundColor Green
}

function Write-Warning {
    param([string]$Message)
    Write-Host "[WARNING] $Message" -ForegroundColor Yellow
}

function Write-Error {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

# Check if Docker is installed
try {
    $null = docker --version
} catch {
    Write-Error "Docker is not installed. Please install Docker Desktop for Windows first."
    Write-Info "Download from: https://www.docker.com/products/docker-desktop"
    exit 1
}

# Check if Docker is running
try {
    $null = docker ps 2>&1
} catch {
    Write-Error "Docker is not running. Please start Docker Desktop."
    exit 1
}

# Check if Docker Compose is installed
$dockerComposeCmd = $null
try {
    $null = docker compose version 2>&1
    $dockerComposeCmd = "docker compose"
} catch {
    try {
        $null = docker-compose --version 2>&1
        $dockerComposeCmd = "docker-compose"
    } catch {
        Write-Error "Docker Compose is not installed. Please install Docker Compose."
        exit 1
    }
}

Write-Info "Starting Amor Document Processor on Windows..."

# Check if .env file exists
if (-not (Test-Path .env)) {
    Write-Warning ".env file not found. Creating from .env.example..."
    if (Test-Path .env.example) {
        Copy-Item .env.example .env
        Write-Info "Please update the .env file with your API keys and configuration."
    } else {
        Write-Error ".env.example not found. Cannot create .env file."
        exit 1
    }
}

# Create data directory if it doesn't exist
if (-not (Test-Path data)) {
    Write-Info "Creating data directory..."
    New-Item -ItemType Directory -Path data | Out-Null
}

# Pull latest images
Write-Info "Pulling latest Docker images..."
if ($dockerComposeCmd -eq "docker compose") {
    docker compose -f docker-compose.yml -f docker-compose.windows.yml pull
} else {
    docker-compose -f docker-compose.yml -f docker-compose.windows.yml pull
}

# Build the application
Write-Info "Building application Docker image..."
if ($dockerComposeCmd -eq "docker compose") {
    docker compose -f docker-compose.yml -f docker-compose.windows.yml build
} else {
    docker-compose -f docker-compose.yml -f docker-compose.windows.yml build
}

# Start services
Write-Info "Starting services..."
if ($dockerComposeCmd -eq "docker compose") {
    docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d
} else {
    docker-compose -f docker-compose.yml -f docker-compose.windows.yml up -d
}

# Wait for services to be healthy
Write-Info "Waiting for services to be ready..."
Start-Sleep -Seconds 10

# Pull Ollama model (qwen2.5:7b) for Local AI
Write-Info "Checking Ollama model (qwen2.5:7b)..."
try {
    $ollamaCheck = docker exec amor-ollama ollama list 2>&1
    if ($ollamaCheck -match "qwen2.5:7b") {
        Write-Success "Ollama model qwen2.5:7b is already installed"
    } else {
        Write-Info "Pulling Ollama model qwen2.5:7b (this may take 5-10 minutes)..."
        docker exec amor-ollama ollama pull qwen2.5:7b
        Write-Success "Ollama model qwen2.5:7b installed successfully"
    }
} catch {
    Write-Warning "Could not check/pull Ollama model. Local AI features may not work until model is downloaded."
    Write-Info "You can manually pull the model later with: docker exec amor-ollama ollama pull qwen2.5:7b"
}

# Check service status
Write-Info "Checking service status..."
if ($dockerComposeCmd -eq "docker compose") {
    docker compose -f docker-compose.yml -f docker-compose.windows.yml ps
} else {
    docker-compose -f docker-compose.yml -f docker-compose.windows.yml ps
}

Write-Success "Amor Document Processor is running!"
Write-Host ""
Write-Info "Service URLs:"
Write-Host "  - Web UI: http://localhost:8000"
Write-Host "  - API Docs: http://localhost:8000/docs"
Write-Host "  - Ollama (Local AI): http://localhost:11434"
Write-Host "  - Grafana: http://localhost:3000 (admin/admin123)"
Write-Host "  - Prometheus: http://localhost:9091"
Write-Host ""
Write-Info "The monochrome chat UI is available with three modes:"
Write-Host "  - Research: Comprehensive research with web scraping"
Write-Host "  - Thinking: Deep analytical problem-solving"
Write-Host "  - Coding: Code generation and technical assistance"
Write-Host ""
Write-Info "To view logs: $dockerComposeCmd -f docker-compose.yml -f docker-compose.windows.yml logs -f"
Write-Info "To stop services: $dockerComposeCmd -f docker-compose.yml -f docker-compose.windows.yml down"
Write-Info "To stop and remove volumes: $dockerComposeCmd -f docker-compose.yml -f docker-compose.windows.yml down -v"
