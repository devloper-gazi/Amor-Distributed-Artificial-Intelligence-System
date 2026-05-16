# Cycle G G2 spike — SGLang multi-tenant feasibility.
#
# Goal: see whether SGLang's RadixAttention prefix cache beats
# llama-swap's --cache-reuse 256 on AMOR's 4-concurrent-build-session
# pattern by ≥ 1.5× (Plan-agent kill ratio).  If yes → migrate.  If
# no → delete the spike branch.
#
# 8 GB VRAM caveat (Plan-agent flagged HIGH risk)
# -----------------------------------------------
# vLLM / SGLang's paged-attention pre-allocates a contiguous KV cache
# block per concurrent request.  On 8 GB after Qwen2.5-Coder-7B Q4
# weights (~5 GB) + KV at Q8_0 (>2 GB at 16K ctx), there's <1 GB free
# for paged blocks.  Expected OOM at concurrency >1.  The spike bench
# starts with `--concurrency 4` to see if SGLang can even RUN under
# AMOR's nominal pattern; OOM here triggers the kill criterion
# (challenger throughput = 0 < 1.5× × incumbent).

FROM lmsysorg/sglang:v0.4.3

# Models bind-mounted from host at /models (same path as llama-swap)
# so we don't ship them inside the image.
WORKDIR /app

# Spike container only — no ENTRYPOINT to avoid accidental docker-run
# spinning up real inference.  Use the launcher.sh script explicitly.
CMD ["bash", "-c", "echo 'Run via launcher.sh; do not docker run this directly.' && sleep infinity"]
