#!/bin/bash

# Amor Document Processor - Linux/Mac Startup Script
# This script starts the document processing system with all required services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    print_error "Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if Docker Compose is installed
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    print_error "Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

# Detect Docker Compose command (v1 vs v2)
if docker compose version &> /dev/null; then
    DOCKER_COMPOSE="docker compose"
else
    DOCKER_COMPOSE="docker-compose"
fi

print_info "Starting Amor Document Processor..."

# Check if .env file exists
if [ ! -f .env ]; then
    print_warning ".env file not found. Creating from .env.example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        print_info "Please update the .env file with your API keys and configuration."
    else
        print_error ".env.example not found. Cannot create .env file."
        exit 1
    fi
fi

# Create data directory if it doesn't exist
if [ ! -d "data" ]; then
    print_info "Creating data directory..."
    mkdir -p data
fi

# Pull latest images
print_info "Pulling latest Docker images..."
$DOCKER_COMPOSE pull

# Build the application
print_info "Building application Docker image..."
$DOCKER_COMPOSE build

# Start services
print_info "Starting services..."
$DOCKER_COMPOSE up -d

# Wait for services to be healthy
print_info "Waiting for services to be ready..."
sleep 10

# Pull Ollama model (qwen2.5:7b) for Local AI
print_info "Checking Ollama model (qwen2.5:7b)..."
if docker exec amor-ollama ollama list 2>/dev/null | grep -q "qwen2.5:7b"; then
    print_success "Ollama model qwen2.5:7b is already installed"
else
    print_info "Pulling Ollama model qwen2.5:7b (this may take 5-10 minutes)..."
    if docker exec amor-ollama ollama pull qwen2.5:7b; then
        print_success "Ollama model qwen2.5:7b installed successfully"
    else
        print_warning "Could not pull Ollama model. Local AI features may not work until model is downloaded."
        print_info "You can manually pull the model later with: docker exec amor-ollama ollama pull qwen2.5:7b"
    fi
fi

# Check service status
print_info "Checking service status..."
$DOCKER_COMPOSE ps

print_success "Amor Document Processor is running!"
echo ""
print_info "Service URLs:"
echo "  - API: http://localhost:8000"
echo "  - Metrics: http://localhost:9090"
echo "  - Grafana: http://localhost:3000 (admin/admin123)"
echo "  - Prometheus: http://localhost:9091"
echo ""
print_info "To view logs: $DOCKER_COMPOSE logs -f"
print_info "To stop services: $DOCKER_COMPOSE down"
print_info "To stop and remove volumes: $DOCKER_COMPOSE down -v"
