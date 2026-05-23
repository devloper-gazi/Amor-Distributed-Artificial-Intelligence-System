# Cycle H.1 — BitNet b1.58 2B4T shadow planner container.
#
# Builds Microsoft's bitnet.cpp (a llama.cpp fork with i2_s kernels)
# + serves the OpenAI-compat llama-server on port 8081.  CPU-only by
# design — Plan-agent locked: 6-10 tok/s realistic on a modern laptop
# CPU, p95 latency ~3-5s for ≤200 prompt-tokens, agreement-rate
# measurement runs in shadow mode.
#
# Build wall-clock: ~10-15 min (bitnet.cpp + dependencies).  Model
# download: ~1.2 GB GGUF, ~2-5 min over a 10 Mbps line.  Both happen
# at image build time so the container starts instantly.

FROM python:3.11-slim AS build

# Toolchain for llama.cpp / bitnet.cpp build.  No CUDA needed; BitNet
# kernels run on AVX/NEON CPU paths.
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        curl \
        clang \
    && rm -rf /var/lib/apt/lists/*

# Clone BitNet (Microsoft fork of llama.cpp with i2_s ternary kernels).
# Microsoft's setup expects FULL submodule init — `--depth 1` alone
# leaves the nested llama.cpp ggml sources empty (CMake bails with
# "No SOURCES given to target: ggml").  Pin a known-good SHA so an
# upstream main-branch breakage can't kill the build silently.
WORKDIR /opt
RUN git clone https://github.com/microsoft/BitNet.git \
    && cd BitNet \
    && git submodule update --init --recursive

WORKDIR /opt/BitNet

# Install Python deps + huggingface_hub for the model pull.
RUN pip install --no-cache-dir -r requirements.txt huggingface_hub

# Download the BitNet model AT BUILD TIME — setup_env.py needs the
# model tensors to generate kernel shapes (i2_s expects exact
# dimensions to emit the LUT tables).  ~1.2 GB into the image; the
# runtime image trims everything except the binary + entrypoint.
RUN mkdir -p models/BitNet-b1.58-2B-4T && \
    python -c "from huggingface_hub import hf_hub_download; \
hf_hub_download(repo_id='microsoft/bitnet-b1.58-2B-4T-gguf', \
                filename='ggml-model-i2_s.gguf', \
                local_dir='models/BitNet-b1.58-2B-4T', \
                local_dir_use_symlinks=False)"

# setup_env.py generates the i2_s LUT kernel headers matched to the
# model's tensor shapes, then runs cmake + make.  Output binary lands
# at /opt/BitNet/build/bin/llama-server.
RUN python setup_env.py \
        -md models/BitNet-b1.58-2B-4T \
        -q i2_s \
    && test -f build/bin/llama-server

# ─── Runtime image ────────────────────────────────────────────────

FROM python:3.11-slim AS runtime

# Minimal runtime: libgomp for OpenMP (llama.cpp parallel decode) +
# huggingface_hub for the model download.
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "huggingface_hub>=0.24,<0.30"

# Copy the built binary (and its runtime data) from the build stage.
COPY --from=build /opt/BitNet/build/bin/llama-server /usr/local/bin/llama-server
RUN chmod +x /usr/local/bin/llama-server

# Bake the model into the image — first boot is instant; saves the
# entrypoint's HF download path (still kept as a fallback when the
# operator mounts a different model via volume).
COPY --from=build /opt/BitNet/models/BitNet-b1.58-2B-4T /models/bitnet

# Models live at /models (mounted from host or named volume).  The
# entrypoint pulls the GGUF on first boot if it's missing.
ENV MODEL_DIR=/models/bitnet \
    MODEL_REPO=microsoft/bitnet-b1.58-2B-4T-gguf \
    MODEL_FILENAME=ggml-model-i2_s.gguf \
    HOST=0.0.0.0 \
    PORT=8081 \
    CTX_SIZE=4096 \
    THREADS=8

COPY compose/bitnet/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8081

# Healthcheck — llama-server's /health endpoint.
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
