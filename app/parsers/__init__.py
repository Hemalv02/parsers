"""Per-format parsers, each a BaseParser subclass. See registry.py for the
extension → parser mapping used by the API and the subprocess worker."""

from .base import BaseParser, ParseResult
from .registry import ALL_SUPPORTED, LEGACY, NATIVE, get_parser

__all__ = ["BaseParser", "ParseResult", "ALL_SUPPORTED", "LEGACY", "NATIVE", "get_parser"]
