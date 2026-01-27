#!/bin/bash
#
# Download models for the Multilingual Web Scraping and Content Synthesis System
#
# Models downloaded:
# - NLLB-200 CTranslate2 model (600MB distilled version)
# - FastText language detection model
# - Nomic embedding model (via sentence-transformers)
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
MODELS_DIR="${MODELS_DIR:-/models}"
NLLB_MODEL_PATH="${NLLB_MODEL_PATH:-$MODELS_DIR/nllb-200-distilled-600M-ct2}"
FASTTEXT_MODEL_PATH="${FASTTEXT_MODEL_PATH:-$MODELS_DIR/lid.176.bin}"
NOMIC_MODEL_PATH="${NOMIC_MODEL_PATH:-$MODELS_DIR/nomic-embed-text-v1.5}"

echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}Model Download Script${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
echo "Models directory: $MODELS_DIR"
echo ""

# Create models directory
mkdir -p "$MODELS_DIR"

# ============================================================================
# Download NLLB-200 CTranslate2 Model
# ============================================================================
download_nllb() {
    echo -e "${YELLOW}[1/3] Downloading NLLB-200 CTranslate2 model...${NC}"
    
    if [ -d "$NLLB_MODEL_PATH" ] && [ -f "$NLLB_MODEL_PATH/model.bin" ]; then
        echo -e "${GREEN}NLLB model already exists at $NLLB_MODEL_PATH${NC}"
        return 0
    fi
    
    # Install ct2-transformers-converter if not present
    if ! command -v ct2-transformers-converter &> /dev/null; then
        echo "Installing CTranslate2..."
        pip install ctranslate2 transformers sentencepiece --quiet
    fi
    
    # Create temporary directory for conversion
    TMP_DIR=$(mktemp -d)
    
    echo "Downloading and converting NLLB-200 model (this may take a while)..."
    
    # Convert model to CTranslate2 format with INT8 quantization
    ct2-transformers-converter \
        --model facebook/nllb-200-distilled-600M \
        --output_dir "$NLLB_MODEL_PATH" \
        --quantization int8_float16 \
        --copy_files tokenizer.json tokenizer_config.json special_tokens_map.json \
        --force
    
    # Copy sentencepiece model if needed
    if [ ! -f "$NLLB_MODEL_PATH/sentencepiece.model" ]; then
        echo "Downloading sentencepiece model..."
        python3 -c "
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained('facebook/nllb-200-distilled-600M')
tokenizer.save_pretrained('$NLLB_MODEL_PATH')
"
    fi
    
    # Cleanup
    rm -rf "$TMP_DIR"
    
    echo -e "${GREEN}NLLB model downloaded to $NLLB_MODEL_PATH${NC}"
}

# ============================================================================
# Download FastText Language Detection Model
# ============================================================================
download_fasttext() {
    echo -e "${YELLOW}[2/3] Downloading FastText language detection model...${NC}"
    
    if [ -f "$FASTTEXT_MODEL_PATH" ]; then
        echo -e "${GREEN}FastText model already exists at $FASTTEXT_MODEL_PATH${NC}"
        return 0
    fi
    
    # Download FastText language identification model (176 languages)
    echo "Downloading lid.176.bin (131MB)..."
    curl -L -o "$FASTTEXT_MODEL_PATH" \
        "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
    
    echo -e "${GREEN}FastText model downloaded to $FASTTEXT_MODEL_PATH${NC}"
}

# ============================================================================
# Download Nomic Embedding Model
# ============================================================================
download_nomic() {
    echo -e "${YELLOW}[3/3] Pre-downloading Nomic embedding model...${NC}"
    
    if [ -d "$NOMIC_MODEL_PATH" ]; then
        echo -e "${GREEN}Nomic model already exists at $NOMIC_MODEL_PATH${NC}"
        return 0
    fi
    
    # Pre-download the embedding model using sentence-transformers
    python3 -c "
from sentence_transformers import SentenceTransformer
import os

print('Downloading nomic-ai/nomic-embed-text-v1.5...')
model = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)

# Save to specified path
model.save('$NOMIC_MODEL_PATH')
print('Model saved to $NOMIC_MODEL_PATH')
"
    
    echo -e "${GREEN}Nomic embedding model downloaded to $NOMIC_MODEL_PATH${NC}"
}

# ============================================================================
# Download Cross-Encoder Reranker Model
# ============================================================================
download_reranker() {
    echo -e "${YELLOW}[Bonus] Pre-downloading cross-encoder reranker model...${NC}"
    
    RERANKER_PATH="$MODELS_DIR/ms-marco-MiniLM-L-6-v2"
    
    if [ -d "$RERANKER_PATH" ]; then
        echo -e "${GREEN}Reranker model already exists at $RERANKER_PATH${NC}"
        return 0
    fi
    
    python3 -c "
from sentence_transformers import CrossEncoder
import os

print('Downloading cross-encoder/ms-marco-MiniLM-L-6-v2...')
model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')

# Save to specified path (CrossEncoder doesn't have save method, but downloading caches it)
print('Model cached in transformers cache')
"
    
    echo -e "${GREEN}Reranker model cached${NC}"
}

# ============================================================================
# Verify Downloads
# ============================================================================
verify_downloads() {
    echo ""
    echo -e "${YELLOW}Verifying downloads...${NC}"
    echo ""
    
    ERRORS=0
    
    # Check NLLB
    if [ -f "$NLLB_MODEL_PATH/model.bin" ]; then
        SIZE=$(du -sh "$NLLB_MODEL_PATH" | cut -f1)
        echo -e "${GREEN}✓ NLLB model: $NLLB_MODEL_PATH ($SIZE)${NC}"
    else
        echo -e "${RED}✗ NLLB model not found${NC}"
        ERRORS=$((ERRORS + 1))
    fi
    
    # Check FastText
    if [ -f "$FASTTEXT_MODEL_PATH" ]; then
        SIZE=$(du -sh "$FASTTEXT_MODEL_PATH" | cut -f1)
        echo -e "${GREEN}✓ FastText model: $FASTTEXT_MODEL_PATH ($SIZE)${NC}"
    else
        echo -e "${RED}✗ FastText model not found${NC}"
        ERRORS=$((ERRORS + 1))
    fi
    
    # Check Nomic
    if [ -d "$NOMIC_MODEL_PATH" ]; then
        SIZE=$(du -sh "$NOMIC_MODEL_PATH" | cut -f1)
        echo -e "${GREEN}✓ Nomic embedding model: $NOMIC_MODEL_PATH ($SIZE)${NC}"
    else
        echo -e "${YELLOW}! Nomic model will be downloaded on first use${NC}"
    fi
    
    echo ""
    
    if [ $ERRORS -eq 0 ]; then
        echo -e "${GREEN}All required models downloaded successfully!${NC}"
    else
        echo -e "${RED}$ERRORS model(s) failed to download${NC}"
        exit 1
    fi
}

# ============================================================================
# Main
# ============================================================================
main() {
    echo "Starting model downloads..."
    echo ""
    
    # Parse arguments
    SKIP_NLLB=false
    SKIP_FASTTEXT=false
    SKIP_NOMIC=false
    
    for arg in "$@"; do
        case $arg in
            --skip-nllb)
                SKIP_NLLB=true
                ;;
            --skip-fasttext)
                SKIP_FASTTEXT=true
                ;;
            --skip-nomic)
                SKIP_NOMIC=true
                ;;
            --help|-h)
                echo "Usage: $0 [OPTIONS]"
                echo ""
                echo "Options:"
                echo "  --skip-nllb      Skip NLLB model download"
                echo "  --skip-fasttext  Skip FastText model download"
                echo "  --skip-nomic     Skip Nomic embedding model download"
                echo ""
                echo "Environment variables:"
                echo "  MODELS_DIR         Base directory for models (default: /models)"
                echo "  NLLB_MODEL_PATH    Path for NLLB model"
                echo "  FASTTEXT_MODEL_PATH Path for FastText model"
                echo ""
                exit 0
                ;;
        esac
    done
    
    # Download models
    if [ "$SKIP_NLLB" = false ]; then
        download_nllb
    else
        echo -e "${YELLOW}Skipping NLLB download${NC}"
    fi
    
    if [ "$SKIP_FASTTEXT" = false ]; then
        download_fasttext
    else
        echo -e "${YELLOW}Skipping FastText download${NC}"
    fi
    
    if [ "$SKIP_NOMIC" = false ]; then
        download_nomic
    else
        echo -e "${YELLOW}Skipping Nomic download${NC}"
    fi
    
    # Verify
    verify_downloads
    
    echo ""
    echo -e "${GREEN}======================================${NC}"
    echo -e "${GREEN}Model download complete!${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo ""
    echo "Environment variables to set:"
    echo "  export NLLB_MODEL_PATH=$NLLB_MODEL_PATH"
    echo "  export FASTTEXT_MODEL_PATH=$FASTTEXT_MODEL_PATH"
    echo ""
}

main "$@"
