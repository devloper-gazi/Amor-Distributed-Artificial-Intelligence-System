"""
Cycle C Sprint 6 — modest fine-tuning loop (ORPO + manual gate).

Pieces
------
* ``export_pairs_jsonl``  — pulls untrained preference pairs from
  Postgres and writes them to a JSONL the trainer ingests.
* ``orpo_qwen_coder``     — Unsloth+TRL ORPOTrainer driver.
* ``convert_lora_gguf``   — wraps llama.cpp's ``convert-lora-to-gguf.py``.

Each script is self-contained and uses only ``argparse`` so they run
unchanged inside the app container, on bare metal, or on a CI runner.
"""
