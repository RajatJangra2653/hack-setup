"""Automated Admin/Trainer Guide document generator.

Generates a professional Word document (.docx) for a hack based on its
state from blob storage.  The document adapts dynamically based on
assigned licenses, users, teams, and hack configuration.
"""
from .generator import DocGenerator
from .templates import LICENSE_SECTION_REGISTRY

__all__ = ["DocGenerator", "LICENSE_SECTION_REGISTRY"]
