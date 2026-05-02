"""
``local_ai.memory`` — Phase 16 Letta-style 3-tier memory hierarchy.

Public surface:

* ``MemoryStore`` — orchestrator; ``read_core`` / ``write_core`` /
  ``patch_core`` / ``append_recall`` / ``search_recall`` /
  ``archive`` / ``search_archival`` / ``stats`` / ``clear_all``.
* Backends: ``CoreMemoryBackend``, ``RecallMemoryBackend``,
  ``ArchivalMemoryBackend``.
* Dataclasses: ``RecallEntry``, ``ArchivalEntry``, ``MemoryStats``.
* Tools (Tool ABC subclasses): ``CoreReadTool``, ``CoreWriteTool``,
  ``CorePatchTool``, ``RecallAppendTool``, ``RecallSearchTool``,
  ``ArchiveTool``, ``ArchivalSearchTool``, ``all_memory_tools``.
* ``make_no_op_store`` — lazy default for agent DI.

Settings (from ``document_processor.config.settings``):
* ``memory_archival_table: str = "amor_archival"`` (reserved for
  later LanceDB-backed archival; SQLite is the Phase 16 default)
* ``memory_recall_window: int = 50``
* ``memory_core_max_bytes: int = 2048``
* ``memory_ledger_audit_enabled: bool = True``
"""

from .archival_backend import (
    ArchivalEntry,
    ArchivalMemoryBackend,
    EmbedderFn,
)
from .core_backend import CoreMemoryBackend
from .recall_backend import RecallEntry, RecallMemoryBackend
from .store import LedgerHookFn, MemoryStats, MemoryStore, make_no_op_store
from .tools import (
    ArchivalSearchTool,
    ArchiveTool,
    CorePatchTool,
    CoreReadTool,
    CoreWriteTool,
    RecallAppendTool,
    RecallSearchTool,
    all_memory_tools,
)


__all__ = [
    # backends
    "CoreMemoryBackend",
    "RecallMemoryBackend",
    "ArchivalMemoryBackend",
    "EmbedderFn",
    # dataclasses
    "RecallEntry",
    "ArchivalEntry",
    "MemoryStats",
    # orchestrator
    "MemoryStore",
    "LedgerHookFn",
    "make_no_op_store",
    # tools
    "CoreReadTool",
    "CoreWriteTool",
    "CorePatchTool",
    "RecallAppendTool",
    "RecallSearchTool",
    "ArchiveTool",
    "ArchivalSearchTool",
    "all_memory_tools",
]
