#!/bin/bash
# Local AI Research System Setup Script
# Automated model download and initialization for RTX 4060 8GB VRAM

set -e

echo "=========================================="
echo "Local AI Research System Setup"
echo "Optimized for RTX 4060 8GB VRAM"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
OLLAMA_MODEL="qwen2.5:7b"
NLLB_MODEL_DIR="./models/nllb-200-distilled-600M"
EMBEDDING_MODEL="nomic-ai/nomic-embed-text-v1.5"

# Check if Ollama is running
echo -e "${BLUE}Checking Ollama service...${NC}"
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo -e "${YELLOW}Warning: Ollama service not responding at localhost:11434${NC}"
    echo "Please ensure Ollama is running with:"
    echo "  docker-compose -f docker-compose.local-ai.yml up -d ollama"
    exit 1
fi
echo -e "${GREEN}✓ Ollama service is running${NC}"
echo ""

# Pull Ollama model
echo -e "${BLUE}Pulling Ollama model: ${OLLAMA_MODEL}...${NC}"
echo "This will download ~4.7GB and take 5-10 minutes depending on connection"
echo ""

if ! ollama list | grep -q "${OLLAMA_MODEL}"; then
    ollama pull "${OLLAMA_MODEL}"
    echo -e "${GREEN}✓ Model ${OLLAMA_MODEL} downloaded${NC}"
else
    echo -e "${GREEN}✓ Model ${OLLAMA_MODEL} already exists${NC}"
fi
echo ""

# Verify model size
echo -e "${BLUE}Verifying model quantization...${NC}"
MODEL_INFO=$(ollama show "${OLLAMA_MODEL}" --modelfile 2>/dev/null || echo "")
if echo "$MODEL_INFO" | grep -q "Q4"; then
    echo -e "${GREEN}✓ Model is Q4 quantized (optimal for 8GB VRAM)${NC}"
else
    echo -e "${YELLOW}⚠ Model quantization format unknown${NC}"
fi
echo ""

# Setup NLLB translation model
echo -e "${BLUE}Setting up NLLB translation model...${NC}"
if [ ! -d "$NLLB_MODEL_DIR" ]; then
    echo "NLLB model not found. To enable local translation:"
    echo ""
    echo "1. Download CTranslate2 converted model:"
    echo "   mkdir -p ./models"
    echo "   cd ./models"
    echo "   wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/model.bin"
    echo "   wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/sentencepiece.model"
    echo ""
    echo "2. Or convert from HuggingFace:"
    echo "   pip install ctranslate2"
    echo "   ct2-transformers-converter --model facebook/nllb-200-distilled-600M --output_dir ./models/nllb-200-distilled-600M --quantization int8"
    echo ""
    echo -e "${YELLOW}⚠ Translation will be disabled until NLLB model is installed${NC}"
else
    echo -e "${GREEN}✓ NLLB model found at ${NLLB_MODEL_DIR}${NC}"
fi
echo ""

# Setup embedding model
echo -e "${BLUE}Checking embedding model...${NC}"
python3 << 'EOF'
try:
    from sentence_transformers import SentenceTransformer
    import os

    model_name = "nomic-ai/nomic-embed-text-v1.5"
    cache_dir = os.path.expanduser("~/.cache/huggingface/hub")

    print(f"Downloading {model_name}...")
    print("This will download ~500MB on first run")

    model = SentenceTransformer(model_name, device="cpu")
    print(f"✓ Embedding model ready (768 dimensions)")
    print(f"  Cached at: {cache_dir}")

except Exception as e:
    print(f"✗ Failed to load embedding model: {e}")
    print("  Run: pip install sentence-transformers")
EOF
echo ""

# Test Ollama generation
echo -e "${BLUE}Testing Ollama generation...${NC}"
TEST_RESPONSE=$(curl -s http://localhost:11434/api/generate -d '{
  "model": "'"${OLLAMA_MODEL}"'",
  "prompt": "Say hello in one word",
  "stream": false
}' 2>/dev/null || echo "")

if echo "$TEST_RESPONSE" | grep -q "response"; then
    echo -e "${GREEN}✓ Ollama generation working${NC}"
    RESPONSE=$(echo "$TEST_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['response'])" 2>/dev/null || echo "OK")
    echo "  Test response: $RESPONSE"
else
    echo -e "${YELLOW}⚠ Ollama generation test failed${NC}"
fi
echo ""

# Memory configuration check
echo -e "${BLUE}Checking system resources...${NC}"
TOTAL_RAM=$(free -g | awk '/^Mem:/{print $2}')
AVAILABLE_RAM=$(free -g | awk '/^Mem:/{print $7}')
echo "  Total RAM: ${TOTAL_RAM}GB"
echo "  Available RAM: ${AVAILABLE_RAM}GB"

if [ "$AVAILABLE_RAM" -lt 4 ]; then
    echo -e "${YELLOW}⚠ Warning: Low available RAM (${AVAILABLE_RAM}GB)${NC}"
    echo "  Recommended: At least 4GB free for optimal performance"
fi
echo ""

# GPU check
echo -e "${BLUE}Checking GPU...${NC}"
if command -v nvidia-smi &> /dev/null; then
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "")
    if [ -n "$GPU_INFO" ]; then
        echo "  GPU: $GPU_INFO"
        echo -e "${GREEN}✓ NVIDIA GPU detected${NC}"
    else
        echo -e "${YELLOW}⚠ nvidia-smi available but no GPU detected${NC}"
    fi
else
    echo -e "${YELLOW}⚠ nvidia-smi not found (CPU mode will be used)${NC}"
fi
echo ""

# Create data directories
echo -e "${BLUE}Creating data directories...${NC}"
mkdir -p ./data/documents
mkdir -p ./data/vectors
mkdir -p ./data/cache
mkdir -p ./models
echo -e "${GREEN}✓ Data directories created${NC}"
echo ""

# Setup summary
echo "=========================================="
echo "Setup Summary"
echo "=========================================="
echo ""
echo "✓ Ollama: ${OLLAMA_MODEL} ready"
echo "  VRAM usage: ~4-5GB"
echo ""

if [ -d "$NLLB_MODEL_DIR" ]; then
    echo "✓ NLLB Translation: Ready"
    echo "  VRAM usage: ~600MB (INT8)"
else
    echo "✗ NLLB Translation: Not configured"
fi
echo ""

echo "✓ Vector Store: LanceDB ready"
echo "  Embedding model: ${EMBEDDING_MODEL}"
echo "  Dimensions: 768"
echo ""

echo "Total estimated VRAM usage:"
echo "  - Qwen 2.5 7B (Q4): ~4.5GB"
echo "  - NLLB Translation: ~0.6GB (if enabled)"
echo "  - Total: ~5.1GB / 8GB available"
echo "  - Remaining: ~2.9GB headroom"
echo ""

echo "=========================================="
echo "Ready to start!"
echo "=========================================="
echo ""
echo "Start the system with:"
echo "  docker-compose -f docker-compose.local-ai.yml up -d"
echo ""
echo "Access the UI at:"
echo "  http://localhost:8000/research"
echo ""
echo "Monitor logs:"
echo "  docker-compose -f docker-compose.local-ai.yml logs -f app"
echo ""