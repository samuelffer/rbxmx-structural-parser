from __future__ import annotations

__version__ = "0.4.1"
__author__ = "samuelffer"
__license__ = "MIT"

from .generator import create_bundle, generate_summary, ScriptRecord, ContextRecord, AttributeRecord
from .parser import (
    get_name,
    get_properties_node,
    get_source,
    get_value,
    iter_top_level_items,
    parse_attributes,
)
from .deps import (
    build_dependency_graph,
    find_require_calls,
    Node,
    ScriptInfo,
    RequireEdge,
)
from .utils import (
    read_text,
    safe_write_text,
    sanitize_filename,
    strip_junk_before_roblox,
    local_tag,
)

__all__ = [
    # Core pipeline
    "create_bundle",
    "generate_summary",
    # Records
    "ScriptRecord",
    "ContextRecord",
    "AttributeRecord",
    # Parser
    "get_name",
    "get_properties_node",
    "get_source",
    "get_value",
    "iter_top_level_items",
    "parse_attributes",
    # Dependency graph
    "build_dependency_graph",
    "find_require_calls",
    "Node",
    "ScriptInfo",
    "RequireEdge",
    # Utils
    "read_text",
    "safe_write_text",
    "sanitize_filename",
    "strip_junk_before_roblox",
    "local_tag",
]
