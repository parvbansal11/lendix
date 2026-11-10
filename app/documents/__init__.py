"""Deterministic templates for documents, exports and chart artifacts."""

from .renderer import DocumentService
from .templates import TEMPLATES, WATERMARK

__all__ = ["DocumentService", "TEMPLATES", "WATERMARK"]
