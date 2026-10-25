"""JARVIS extension tools, registered into the OpenJarvis ``ToolRegistry``."""

from __future__ import annotations

from jarvis_ext.tools.media_render import TOOL_NAME as _MEDIA_RENDER
from jarvis_ext.tools.media_render import MediaRenderTool
from jarvis_ext.tools.media_validate import TOOL_NAME as _MEDIA_VALIDATE
from jarvis_ext.tools.media_validate import MediaValidateTool
from jarvis_ext.tools.media_workspace import TOOL_NAME as _MEDIA_WORKSPACE
from jarvis_ext.tools.media_workspace import MediaWorkspaceInspectTool
from openjarvis.core.registry import ToolRegistry

_TOOLS = {
    _MEDIA_WORKSPACE: MediaWorkspaceInspectTool,
    _MEDIA_RENDER: MediaRenderTool,
    _MEDIA_VALIDATE: MediaValidateTool,
}


def ensure_registered() -> None:
    """Register each extension tool unless the registry already holds it."""
    for name, tool_cls in _TOOLS.items():
        if not ToolRegistry.contains(name):
            ToolRegistry.register_value(name, tool_cls)


__all__ = [
    "MediaRenderTool",
    "MediaValidateTool",
    "MediaWorkspaceInspectTool",
    "ensure_registered",
]
