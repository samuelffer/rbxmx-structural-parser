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

        for aname, atype, aval in parse_attributes(props):
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
    # ---------------------------

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

    except Exception as e:
        safe_write_text(
            bundle_dir / "DEPENDENCIES_ERROR.txt",
            f"Dependency extraction failed: {e}\n",
            encoding="utf-8",
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
