# Claude Multi-Research Document Processor - Windows Startup Script
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

Write-Info "Starting Claude Multi-Research Document Processor on Windows..."

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

# Check service status
Write-Info "Checking service status..."
if ($dockerComposeCmd -eq "docker compose") {
    docker compose -f docker-compose.yml -f docker-compose.windows.yml ps
} else {
    docker-compose -f docker-compose.yml -f docker-compose.windows.yml ps
}

Write-Success "Claude Multi-Research Document Processor is running!"
Write-Host ""
Write-Info "Service URLs:"
Write-Host "  - API: http://localhost:8000"
Write-Host "  - Metrics: http://localhost:9090"
Write-Host "  - Grafana: http://localhost:3000 (admin/admin123)"
Write-Host "  - Prometheus: http://localhost:9091"
Write-Host ""
Write-Info "To view logs: $dockerComposeCmd -f docker-compose.yml -f docker-compose.windows.yml logs -f"
Write-Info "To stop services: $dockerComposeCmd -f docker-compose.yml -f docker-compose.windows.yml down"
Write-Info "To stop and remove volumes: $dockerComposeCmd -f docker-compose.yml -f docker-compose.windows.yml down -v"
