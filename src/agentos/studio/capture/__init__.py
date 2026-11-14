"""Capture (S2): screen sets, the layout manifest, and capture into the studio store."""

from agentos.studio.capture.capture import MAX_IMAGE_HEIGHT, capture
from agentos.studio.capture.manifest import DEFAULT_CAP, build_manifest, read_manifest
from agentos.studio.capture.screens import DEFAULT_THEMES, screens

__all__ = ["DEFAULT_CAP", "DEFAULT_THEMES", "MAX_IMAGE_HEIGHT", "build_manifest", "capture", "read_manifest",
           "screens"]
