from __future__ import annotations

import csv
import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import xml.etree.ElementTree as ET

from .deps import Node, ScriptInfo, build_dependency_graph
from .parser import (
    get_name,
    get_properties_node,
    get_source,
    get_value,
    iter_top_level_items,
    parse_attributes,
)
from .utils import (
    local_tag,
    read_text,
    safe_open_csv,
    safe_write_text,
    sanitize_filename,
    strip_junk_before_roblox,
    wipe_dir,
)

LOG = logging.getLogger("rbxbundle")

SCRIPT_CLASSES = {"Script", "LocalScript", "ModuleScript"}

CONTEXT_CLASSES = {
    "RemoteEvent",
    "RemoteFunction",
    "BindableEvent",
    "BindableFunction",
    "StringValue",
    "NumberValue",
    "BoolValue",
    "IntValue",
    "ObjectValue",
    "Folder",
    "Configuration",
}

VALUE_OBJECT_CLASSES = {"StringValue", "NumberValue", "BoolValue", "IntValue", "ObjectValue"}

SERVER_ONLY_PREFIXES = (
    "ServerScriptService/",
    "ServerStorage/",
)

CLIENT_ONLY_PREFIXES = (
    "StarterPlayer/",
    "StarterGui/",
    "StarterPack/",
    "ReplicatedFirst/",
)


@dataclass
class ScriptRecord:
    class_name: str
    name: str
    full_path: str
    rel_file: str
    source_len: int


@dataclass
class ContextRecord:
    class_name: str
    name: str
    full_path: str
    details: Dict[str, str]


@dataclass
class AttributeRecord:
    owner_class: str
    owner_name: str
    owner_path: str
    attr_name: str
    attr_type: str
    attr_value: str


def _unique_child_name(parent_used: Dict[str, int], base_safe: str, referent: str) -> str:
    if base_safe not in parent_used:
        parent_used[base_safe] = 1
        return base_safe

    parent_used[base_safe] += 1
    n = parent_used[base_safe]
    tail = sanitize_filename(referent[-8:]) if referent else ""
    return f"{base_safe}__{n}__{tail}" if tail else f"{base_safe}__{n}"


def create_bundle(in_path: Path, *, output_dir: Path, include_context: bool) -> Tuple[Path, Path, List[ScriptRecord]]:
    xml_text = strip_junk_before_roblox(read_text(in_path))

    try:
        root = ET.fromstring(xml_text)
        tree = ET.ElementTree(root)
    except ET.ParseError as e:
        pos = getattr(e, "position", None)
        where = f" (line {pos[0]}, col {pos[1]})" if pos else ""
        raise RuntimeError(f"XML parse error{where}: {e}") from e

    roblox_root = tree.getroot()
    top_items = iter_top_level_items(roblox_root)
    if not top_items:
        raise RuntimeError("No top-level <Item> found. Export may be incomplete/corrupted.")

    bundle_dir = output_dir / f"{in_path.stem}_bundle"
    wipe_dir(bundle_dir)

    scripts_dir = bundle_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    scripts: List[ScriptRecord] = []
    contexts: List[ContextRecord] = []
    attributes: List[AttributeRecord] = []
    hierarchy_lines: List[str] = []

    # Instance index for dependency resolution
    nodes: Dict[str, Node] = {}

    # We'll keep raw script sources (without our header)
    script_sources: Dict[str, str] = {}

    used_names_by_parent: Dict[str, Dict[str, int]] = {}

    def walk(item: ET.Element, parent_path: str, depth: int) -> None:
        class_name = item.attrib.get("class", "UnknownClass")
        referent = item.attrib.get("referent", "")

        props = get_properties_node(item)
        name = get_name(props) or (referent or "Unnamed")

        base_safe = sanitize_filename(name)
        used = used_names_by_parent.setdefault(parent_path, {})
        safe_name = _unique_child_name(used, base_safe, referent)

        full_path = f"{parent_path}/{safe_name}" if parent_path else safe_name

        nodes[full_path] = Node(
            class_name=class_name,
            name=name,
            safe_name=safe_name,
            full_path=full_path,
            parent_path=parent_path,
        )

        hierarchy_lines.append(f"{' ' * depth}- {safe_name} ({class_name})")

        for aname, atype, aval in parse_attributes(
            props,
            source_file=in_path.name,
            section="Properties",
            owner_path=full_path,
        ):
            attributes.append(AttributeRecord(class_name, name, full_path, aname, atype, aval))

        if include_context and class_name in CONTEXT_CLASSES:
            detail: Dict[str, str] = {}
            if class_name in {"RemoteEvent", "RemoteFunction"}:
                detail["kind"] = "Remote"
            elif class_name in {"BindableEvent", "BindableFunction"}:
                detail["kind"] = "Bindable"
            elif class_name in VALUE_OBJECT_CLASSES:
                detail["kind"] = "ValueObject"
                v = get_value(props)
                if v is not None:
                    detail["initial_value"] = v
            else:
                detail["kind"] = "Context"
            contexts.append(ContextRecord(class_name, name, full_path, detail))

        if class_name in SCRIPT_CLASSES:
            src = get_source(props) or ""

            if class_name == "Script":
                suffix = ".server.lua"
            elif class_name == "LocalScript":
                suffix = ".client.lua"
            else:
                suffix = ".lua"

            parts = [sanitize_filename(p) for p in full_path.split("/")]
            rel = Path(*parts[:-1]) / f"{parts[-1]}{suffix}"
            out_file = scripts_dir / rel
            out_file.parent.mkdir(parents=True, exist_ok=True)

            header = (
                "-- Extracted from RBXMX\n"
                f"-- Class: {class_name}\n"
                f"-- Name: {name}\n"
                f"-- Path: {full_path}\n\n"
            )

            safe_write_text(out_file, header + src, encoding="utf-8")

            scripts.append(
                ScriptRecord(
                    class_name=class_name,
                    name=name,
                    full_path=full_path,
                    rel_file=str(Path("scripts") / rel),
                    source_len=len(src),
                )
            )

            script_sources[full_path] = src

        for child in item:
            if local_tag(child.tag) == "Item":
                walk(child, full_path, depth + 1)

    for it in top_items:
        walk(it, "", 0)

    # Core outputs
    safe_write_text(bundle_dir / "HIERARCHY.txt", "\n".join(hierarchy_lines), encoding="utf-8")

    with safe_open_csv(bundle_dir / "INDEX.csv") as f:
        w = csv.writer(f)
        w.writerow(["class", "name", "path", "file", "source_len"])
        for s in scripts:
            w.writerow([s.class_name, s.name, s.full_path, s.rel_file, s.source_len])

    with safe_open_csv(bundle_dir / "ATTRIBUTES.csv") as f:
        w = csv.writer(f)
        w.writerow(["owner_class", "owner_name", "owner_path", "attr_name", "attr_type", "attr_value"])
        for a in attributes:
            w.writerow([a.owner_class, a.owner_name, a.owner_path, a.attr_name, a.attr_type, a.attr_value])

    if attributes:
        lines = ["# Attributes extracted", ""]
        for a in attributes:
            lines.append(f"- {a.owner_path} ({a.owner_class}) :: {a.attr_name} [{a.attr_type}] = {a.attr_value}")
        safe_write_text(bundle_dir / "ATTRIBUTES.txt", "\n".join(lines), encoding="utf-8")
    else:
        safe_write_text(bundle_dir / "ATTRIBUTES.txt", "# Attributes extracted\n\n(none)\n", encoding="utf-8")

    if include_context:
        remotes = [c for c in contexts if c.details.get("kind") == "Remote"]
        bindables = [c for c in contexts if c.details.get("kind") == "Bindable"]
        values = [c for c in contexts if c.details.get("kind") == "ValueObject"]
        others = [c for c in contexts if c.details.get("kind") not in {"Remote", "Bindable", "ValueObject"}]

        lines = ["# Context objects (detailed)", ""]

        if remotes:
            lines += ["## Remotes", ""]
            lines += [f"- {c.full_path} ({c.class_name})" for c in remotes]
            lines.append("")

        if bindables:
            lines += ["## Bindables", ""]
            lines += [f"- {c.full_path} ({c.class_name})" for c in bindables]
            lines.append("")

        if values:
            lines += ["## ValueObjects", ""]
            for c in values:
                iv = c.details.get("initial_value", "")
                lines.append(f"- {c.full_path} ({c.class_name})" + (f" = {iv}" if iv else ""))
            lines.append("")

        if others:
            lines += ["## Other context", ""]
            lines += [f"- {c.full_path} ({c.class_name})" for c in others]
            lines.append("")

        safe_write_text(bundle_dir / "CONTEXT.txt", "\n".join(lines), encoding="utf-8")

    # ---------------------------
    # Dependency graph outputs
    # ----------------------------

    nodes_json: List[dict] = []
    edges_json: List[dict] = []
    dependency_analysis_failed = False

    try:
        dep_scripts = [
            ScriptInfo(
                class_name=s.class_name,
                name=s.name,
                full_path=s.full_path,
                source=script_sources.get(s.full_path, ""),
            )
            for s in scripts
        ]

        nodes_json, edges_json = build_dependency_graph(dep_scripts, nodes)

        dep_payload = {
            "version": 1,
            "nodes": nodes_json,
            "edges": edges_json,
        }

        safe_write_text(
            bundle_dir / "DEPENDENCIES.json",
            json.dumps(dep_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with safe_open_csv(bundle_dir / "EDGES.csv") as f:
            w = csv.writer(f)
            w.writerow(["from", "to", "kind", "confidence", "expr", "line"])
            for e in edges_json:
                loc = e.get("loc") or {}
                w.writerow([
                    e.get("from"),
                    e.get("to"),
                    e.get("kind"),
                    e.get("confidence"),
                    e.get("expr"),
                    loc.get("line"),
                ])

    except (AttributeError, ValueError, TypeError, KeyError) as e:
        LOG.error(
            "dependency_extraction_failed file=%s section=dependencies data_type=graph error=%s",
            in_path.name,
            e,
        )
        dep_error = "\n".join(
            [
                "Dependency extraction failed.",
                f"File: {in_path.name}",
                "Section: dependencies",
                "Data type: graph",
                f"Error type: {type(e).__name__}",
                f"Error: {e}",
                "",
            ]
        )
        safe_write_text(
            bundle_dir / "DEPENDENCIES_ERROR.txt",
            dep_error,
            encoding="utf-8",
        )
        dependency_analysis_failed = True
        nodes_json = []
        edges_json = []

    # SUMMARY.md
    try:
        summary_md = generate_summary(
            source_file=in_path.name,
            scripts=scripts,
            contexts=contexts,
            attributes=attributes,
            nodes_json=nodes_json,
            edges_json=edges_json,
            include_context=include_context,
            dependency_analysis_failed=dependency_analysis_failed,
        )
        safe_write_text(bundle_dir / "SUMMARY.md", summary_md, encoding="utf-8")
    except (KeyError, TypeError, ValueError) as e:
        LOG.warning(
            "summary_generation_failed file=%s section=SUMMARY.md data_type=document error=%s",
            in_path.name,
            e,
        )

    # ZIP bundle
    zip_path = output_dir / f"{in_path.stem}_bundle.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted((bundle_dir / "scripts").rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(bundle_dir)))

        for fname in [
            "SUMMARY.md",
            "HIERARCHY.txt",
            "INDEX.csv",
            "ATTRIBUTES.csv",
            "ATTRIBUTES.txt",
            "DEPENDENCIES.json",
            "EDGES.csv",
            "DEPENDENCIES_ERROR.txt",
        ]:
            p = bundle_dir / fname
            if p.exists():
                z.write(p, arcname=p.name)

        if include_context:
            p = bundle_dir / "CONTEXT.txt"
            if p.exists():
                z.write(p, arcname=p.name)

    return bundle_dir, zip_path, scripts


# ---------------------------------------------------------------------------
# SUMMARY.md generator
# ---------------------------------------------------------------------------

def _confidence_label(conf: float) -> str:
    if conf >= 0.9:
        return "high"
    if conf >= 0.6:
        return "medium"
    return "low"


def _confidence_icon(conf: float) -> str:
    if conf >= 0.9:
        return "✅"
    if conf >= 0.6:
        return "⚠️"
    return "❓"


def generate_summary(
    *,
    source_file: str,
    scripts: List[ScriptRecord],
    contexts: List[ContextRecord],
    attributes: List[AttributeRecord],
    nodes_json: List[dict],
    edges_json: List[dict],
    include_context: bool,
    dependency_analysis_failed: bool = False,
) -> str:
    """Return the content of SUMMARY.md as a string."""

    lines: List[str] = []

    lines += [
        "# RBXBundle — Project Summary",
        "",
        f"**Source file:** `{source_file}`  ",
        f"**Scripts found:** {len(scripts)}  ",
        "",
        "---",
        "",
    ]

    # --- Scripts section ---
    lines += ["## Scripts", ""]

    server_scripts = [s for s in scripts if s.class_name == "Script"]
    local_scripts  = [s for s in scripts if s.class_name == "LocalScript"]
    module_scripts = [s for s in scripts if s.class_name == "ModuleScript"]

    if server_scripts:
        lines += ["### Server Scripts", ""]
        for s in server_scripts:
            empty_tag = " *(empty)*" if s.source_len == 0 else ""
            lines.append(f"- `{s.full_path}`{empty_tag}")
        lines.append("")

    if local_scripts:
        lines += ["### Client Scripts", ""]
        for s in local_scripts:
            empty_tag = " *(empty)*" if s.source_len == 0 else ""
            lines.append(f"- `{s.full_path}`{empty_tag}")
        lines.append("")

    if module_scripts:
        lines += ["### Module Scripts", ""]
        for s in module_scripts:
            empty_tag = " *(empty)*" if s.source_len == 0 else ""
            lines.append(f"- `{s.full_path}`{empty_tag}")
        lines.append("")

    if not scripts:
        lines += ["*(no scripts found)*", ""]

    lines += ["---", ""]

    # --- Dependencies section ---
    lines += ["## Dependency Graph", ""]

    if dependency_analysis_failed:
        lines += ["⚠️ Dependencies cannot be analised; See `DEPENDENCIES_ERROR.txt`.", ""]

    if not edges_json:
        lines += ["*(no require() calls detected)*", ""]
    else:
        resolved   = [e for e in edges_json if e.get("to") is not None]
        unresolved = [e for e in edges_json if e.get("to") is None]

        if resolved:
            lines += ["### Resolved", ""]
            for e in resolved:
                icon = _confidence_icon(e.get("confidence", 0.0))
                conf_pct = int(e.get("confidence", 0.0) * 100)
                kind = e.get("kind", "unknown")
                loc  = e.get("loc") or {}
                line_info = f" *(line {loc['line']})*" if loc.get("line") else ""
                lines.append(
                    f"- {icon} `{e['from']}` → `{e['to']}`  "
                    f"[{kind}, confidence: {conf_pct}%]{line_info}"
                )
            lines.append("")

        if unresolved:
            lines += ["### Unresolved / Dynamic", ""]
            for e in unresolved:
                loc  = e.get("loc") or {}
                line_info = f" *(line {loc['line']})*" if loc.get("line") else ""
                lines.append(
                    f"- ❓ `{e['from']}` → *(unresolved)*  "
                    f"`{e.get('expr', '')}`{line_info}"
                )
            lines.append("")

    lines += ["---", ""]

    # --- Client/Server boundary alerts ---
    lines += ["## Client/Server Boundary Alerts", ""]

    scripts_by_path = {s.full_path: s for s in scripts}
    boundary_alerts: List[str] = []

    for e in edges_json:
        origin = e.get("from")
        dest = e.get("to")
        if not origin or not dest:
            continue

        src_script = scripts_by_path.get(origin)
        if not src_script:
            continue

        loc = e.get("loc") or {}
        line_info = f" (line {loc['line']})" if loc.get("line") else ""

        if src_script.class_name == "LocalScript" and dest.startswith(SERVER_ONLY_PREFIXES):
            boundary_alerts.append(
                f"- ⚠️ `{origin}` -> `{dest}`{line_info} "
                "(LocalScript depending on server-only path)"
            )

        if src_script.class_name == "Script" and dest.startswith(CLIENT_ONLY_PREFIXES):
            boundary_alerts.append(
                f"- ⚠️ `{origin}` -> `{dest}`{line_info} "
                "(Script depending on client-only path)"
            )

    if boundary_alerts:
        lines.extend(boundary_alerts)
    else:
        lines.append("*(no client/server boundary alerts)*")
    lines += ["", "---", ""]


    # --- Context section ---
    if include_context and contexts:
        lines += ["## Context Objects", ""]

        remotes   = [c for c in contexts if c.details.get("kind") == "Remote"]
        bindables = [c for c in contexts if c.details.get("kind") == "Bindable"]
        values    = [c for c in contexts if c.details.get("kind") == "ValueObject"]
        others    = [c for c in contexts if c.details.get("kind") not in {"Remote", "Bindable", "ValueObject"}]

        if remotes:
            lines += [f"**RemoteEvents / RemoteFunctions** ({len(remotes)})", ""]
            for c in remotes:
                lines.append(f"- `{c.full_path}` ({c.class_name})")
            lines.append("")

        if bindables:
            lines += [f"**Bindables** ({len(bindables)})", ""]
            for c in bindables:
                lines.append(f"- `{c.full_path}` ({c.class_name})")
            lines.append("")

        if values:
            lines += [f"**Value Objects** ({len(values)})", ""]
            for c in values:
                iv = c.details.get("initial_value", "")
                val_str = f" = `{iv}`" if iv else ""
                lines.append(f"- `{c.full_path}` ({c.class_name}){val_str}")
            lines.append("")

        if others:
            lines += [f"**Other** ({len(others)})", ""]
            for c in others:
                lines.append(f"- `{c.full_path}` ({c.class_name})")
            lines.append("")

        lines += ["---", ""]

    # --- Attributes section ---
    if attributes:
        lines += [f"## Attributes ({len(attributes)} total)", ""]
        # group by owner
        by_owner: dict = {}
        for a in attributes:
            by_owner.setdefault(a.owner_path, []).append(a)
        for owner_path, attrs in by_owner.items():
            lines.append(f"**`{owner_path}`**")
            for a in attrs:
                lines.append(f"  - `{a.attr_name}` [{a.attr_type}] = `{a.attr_value}`")
        lines += ["", "---", ""]

    # --- Footer ---
    lines += [
        "## How to use this bundle",
        "",
        "1. Upload the `.zip` file (or paste individual files) into your AI tool.",
        "2. Reference specific scripts by their path shown above.",
        "3. Use `HIERARCHY.txt` to understand instance structure.",
        "4. Use `DEPENDENCIES.json` or `EDGES.csv` for script relationships.",
        "5. Use `CONTEXT.txt` for RemoteEvent / ValueObject details.",
        "",
        "> *Generated by [rbxbundle](https://github.com/samuelffer/rbxbundle)*",
        "",
    ]

    return "\n".join(lines)
