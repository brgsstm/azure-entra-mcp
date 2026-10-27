"""Definitions shared by every tool module."""

from mcp.types import ToolAnnotations

# Every tool only reads; openWorldHint because results come from Graph/ARM, not a
# closed local dataset.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
