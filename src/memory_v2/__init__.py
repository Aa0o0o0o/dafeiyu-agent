"""One scoped authority for V2 memory. No transport/provider dependencies."""

from .service import MemoryService

__all__ = ["MemoryService"]
