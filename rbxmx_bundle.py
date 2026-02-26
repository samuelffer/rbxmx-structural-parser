#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

# ----------------- Config -----------------
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
SUPPORTED_EXTS = {".rbxmx", ".xml", ".txt"}

SCRIPT_CLASSES = {"Script", "LocalScript", "ModuleScript"}

# Alguns exports usam ProtectedString; outros podem variar.
SOURCE_PROP_NAME = "Source"
SOURCE_TAG_NAMES = {"ProtectedString", "string", "SharedString"}

# Name normalmente vem em <string name="Name">...</string>
NAME_PROP_TAGS = {"string"}

# Objetos considerados "contexto"
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

# ValueObjects cujo valor inicial faz sentido extrair
VALUE_OBJECT_CLASSES = {
    "StringValue",
    "NumberValue",
    "BoolValue",
    "IntValue",
    "ObjectValue",
}

INVALID_FS_CHARS = r'<>:"/\|?*\0'
INVALID_FS_RE = re.compile(f"[{re.escape(INVALID_FS_CHARS)}]")

# ----------------- Logging (3.1) -----------------
LOG = logging.getLogger("rbxbundle")


def setup_logging() -> None:
    # basicConfig deve ser chamado cedo; nível INFO por padrão.
    # Você pode mudar para DEBUG editando aqui, ou via env/argumentos num passo futuro.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )


# ----------------- Data -----------------
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
    # detalhes extras (2.3): type, initial_value, etc.
    details: Dict[str, str] = field(default_factory=dict)


@dataclass
class AttributeRecord:
    # 2.2: Attributes por Item
    owner_class: str
    owner_name: str
    owner_path: str
    attr_name: str
    attr_type: str
    attr_value: str


# ----------------- Helpers (robust) -----------------
def ensure_dirs() -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Falha ao criar diretórios '{INPUT_DIR}'/'{OUTPUT_DIR}'. "
            f"Verifique permissões. Detalhe: {e}"
        ) from e


def safe_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
    except OSError as e:
        raise RuntimeError(
            f"Falha ao escrever '{path}'. Verifique permissões/espaço. Detalhe: {e}"
        ) from e


def safe_open_csv(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("w", newline="", encoding="utf-8")
    except OSError as e:
        raise RuntimeError(
            f"Falha ao abrir '{path}' para escrita. Verifique permissões. Detalhe: {e}"
        ) from e


def hr() -> None:
    LOG.info("-" * 64)


def sanitize_filename(s: str) -> str:
    s = INVALID_FS_RE.sub("_", s)
    s = s.strip().strip(".")
    return s or "_"


def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise RuntimeError(
            f"Falha ao ler '{path}'. Verifique se existe e permissões. Detalhe: {e}"
        ) from e

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def list_candidates() -> List[Path]:
    files: List[Path] = []
    try:
        for p in sorted(INPUT_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                files.append(p)
    except OSError as e:
        raise RuntimeError(f"Falha ao listar '{INPUT_DIR}': {e}") from e
    return files


def pick_file(files: List[Path]) -> Optional[Path]:
    hr()
    LOG.info("Selecione um arquivo em ./input/")
    hr()
    for i, p in enumerate(files, start=1):
        try:
            size_kb = p.stat().st_size / 1024.0
        except OSError:
            size_kb = 0.0
        LOG.info("[%d] %s (%.1f KB)", i, p.name, size_kb)
    LOG.info("[0] Sair")

    while True:
        s = input("\nNúmero: ").strip()
        if s.isdigit():
            n = int(s)
            if n == 0:
                return None
            if 1 <= n <= len(files):
                return files[n - 1]
        LOG.error("Entrada inválida.")


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    d = "S/n" if default_yes else "s/N"
    while True:
        s = input(f"{prompt} [{d}]: ").strip().lower()
        if not s:
            return default_yes
        if s in ("s", "sim", "y", "yes"):
            return True
        if s in ("n", "nao", "não", "no"):
            return False
        LOG.error("Responda com S ou N.")


def get_properties_node(item: ET.Element) -> Optional[ET.Element]:
    for child in item:
        if local_tag(child.tag) == "Properties":
            return child
    return None


def get_name_from_properties(props: Optional[ET.Element]) -> Optional[str]:
    if props is None:
        return None
    for p in props:
        if local_tag(p.tag) in NAME_PROP_TAGS and p.attrib.get("name") == "Name":
            return p.text or ""
    return None


def get_source_from_properties(props: Optional[ET.Element]) -> Optional[str]:
    if props is None:
        return None
    for p in props:
        if p.attrib.get("name") == SOURCE_PROP_NAME and local_tag(p.tag) in SOURCE_TAG_NAMES:
            return p.text or ""
    return None


def get_value_from_properties(props: Optional[ET.Element]) -> Optional[str]:
    """
    2.3: extrai valor inicial de ValueObjects (Value property).
    No RBXMX, geralmente:
      <string name="Value">...</string>
      <int name="Value">...</int>
      <float name="Value">...</float>
      <bool name="Value">true</bool>
      <Object name="Value">RBXRef</Object> (ou vazio)
    """
    if props is None:
        return None
    for p in props:
        if p.attrib.get("name") == "Value":
            txt = p.text or ""
            return txt.strip()
    return None


def iter_top_level_items(roblox_root: ET.Element) -> List[ET.Element]:
    direct_items = [c for c in list(roblox_root) if local_tag(c.tag) == "Item"]
    for it in direct_items:
        if it.attrib.get("class") == "DataModel":
            return [c for c in list(it) if local_tag(c.tag) == "Item"]
    return direct_items


def strip_junk_before_roblox(xml_text: str) -> str:
    idx = xml_text.find("<roblox")
    if idx > 0:
        return xml_text[idx:]
    return xml_text


# ----------------- 2.2 Attributes parsing -----------------
def parse_attributes(props: Optional[ET.Element]) -> List[Tuple[str, str, str]]:
    """
    Retorna lista (name, type, value).
    Heurística: Attributes podem vir como:
      <Attributes>
        <Attribute name="A" type="string" value="X" />
        <Attribute name="B" type="number">1</Attribute>
      </Attributes>
    """
    if props is None:
        return []

    out: List[Tuple[str, str, str]] = []

    for node in props:
        if local_tag(node.tag) != "Attributes":
            continue

        for attr in list(node):
            if local_tag(attr.tag) != "Attribute":
                continue

            aname = (attr.attrib.get("name") or "").strip()
            atype = (attr.attrib.get("type") or "").strip()
            aval = attr.attrib.get("value")

            if aval is None:
                aval = (attr.text or "").strip()
            else:
                aval = str(aval).strip()

            if aname:
                out.append((aname, atype or "unknown", aval))

    return out


# ----------------- 2.4 Duplicate name handling -----------------
def unique_child_name(parent_used: Dict[str, int], base_safe: str, referent: str) -> str:
    """
    Garante unicidade de nomes no MESMO nível.
    Se já existe, suffix com contador + pedacinho do referent (mais estável).
    """
    key = base_safe
    if key not in parent_used:
        parent_used[key] = 1
        return base_safe

    parent_used[key] += 1
    n = parent_used[key]

    ref_tail = sanitize_filename(referent[-8:]) if referent else ""
    if ref_tail:
        return f"{base_safe}__{n}__{ref_tail}"
    return f"{base_safe}__{n}"


def build_bundle(in_path: Path, include_context: bool) -> Tuple[Path, Path, List[ScriptRecord]]:
    xml_text = strip_junk_before_roblox(read_text(in_path))

    try:
        root = ET.fromstring(xml_text)
        tree = ET.ElementTree(root)
    except ET.ParseError as e:
        pos = getattr(e, "position", None)
        where = f" (linha {pos[0]}, coluna {pos[1]})" if pos else ""
        raise RuntimeError(f"Erro ao parsear XML{where}: {e}") from e

    roblox_root = tree.getroot()
    if local_tag(roblox_root.tag).lower() != "roblox":
        LOG.warning("Aviso: raiz do XML não é <roblox>. Tentando continuar...")

    top_items = iter_top_level_items(roblox_root)
    if not top_items:
        raise RuntimeError("Não encontrei <Item> de topo no XML. Export pode estar incompleto/corrompido.")

    bundle_dir = OUTPUT_DIR / f"{in_path.stem}_bundle"
    scripts_dir = bundle_dir / "scripts"

    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"Falha ao criar diretórios em '{bundle_dir}': {e}") from e

    scripts: List[ScriptRecord] = []
    contexts: List[ContextRecord] = []
    attributes: List[AttributeRecord] = []
    hierarchy_lines: List[str] = []

    # mapa de usados por "parent_path" para tratar duplicados (2.4)
    used_names_by_parent: Dict[str, Dict[str, int]] = {}

    def walk(item: ET.Element, parent_path: str, depth: int) -> None:
        class_name = item.attrib.get("class", "UnknownClass")
        referent = item.attrib.get("referent", "")
        props = get_properties_node(item)

        name = get_name_from_properties(props)
        if name is None:
            name = referent or "Unnamed"

        base_safe = sanitize_filename(name)
        used = used_names_by_parent.setdefault(parent_path, {})
        safe_name = unique_child_name(used, base_safe, referent)

        full_path = f"{parent_path}/{safe_name}" if parent_path else safe_name
        indent = " " * depth
        hierarchy_lines.append(f"{indent}- {safe_name} ({class_name})")

        # 2.2: Attributes por item
        for aname, atype, aval in parse_attributes(props):
            attributes.append(
                AttributeRecord(
                    owner_class=class_name,
                    owner_name=name,
                    owner_path=full_path,
                    attr_name=aname,
                    attr_type=atype,
                    attr_value=aval,
                )
            )

        # 2.3: Contexto mais detalhado
        if include_context and class_name in CONTEXT_CLASSES:
            detail: Dict[str, str] = {}
            if class_name in {"RemoteEvent", "RemoteFunction"}:
                detail["kind"] = "Remote"
            elif class_name in {"BindableEvent", "BindableFunction"}:
                detail["kind"] = "Bindable"
            elif class_name in VALUE_OBJECT_CLASSES:
                detail["kind"] = "ValueObject"
                v = get_value_from_properties(props)
                if v is not None:
                    detail["initial_value"] = v
            else:
                detail["kind"] = "Context"

            contexts.append(ContextRecord(class_name=class_name, name=name, full_path=full_path, details=detail))

        # Scripts
        if class_name in SCRIPT_CLASSES:
            src = get_source_from_properties(props) or ""
            suffix = (
                ".server.lua" if class_name == "Script"
                else ".client.lua" if class_name == "LocalScript"
                else ".lua"
            )

            # 2.4: nome do arquivo exportado deve ser único (usa full_path “achatado”)
            # (além do esquema por pastas; isso elimina qualquer colisão residual)
            flat = sanitize_filename(full_path.replace("/", "_"))
            rel = Path(flat).with_suffix(suffix)
            out_file = scripts_dir / rel

            header = (
                f"-- Extracted from RBXMX\n"
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

        for child in item:
            if local_tag(child.tag) == "Item":
                walk(child, full_path, depth + 1)

    for it in top_items:
        walk(it, parent_path="", depth=0)

    # HIERARCHY
    safe_write_text(bundle_dir / "HIERARCHY.txt", "\n".join(hierarchy_lines), encoding="utf-8")

    # INDEX
    with safe_open_csv(bundle_dir / "INDEX.csv") as f:
        w = csv.writer(f)
        w.writerow(["class", "name", "path", "file", "source_len"])
        for s in scripts:
            w.writerow([s.class_name, s.name, s.full_path, s.rel_file, s.source_len])

    # 2.2: export attributes (CSV + TXT)
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

    # 2.3: CONTEXT detalhado
    if include_context:
        remotes = [c for c in contexts if c.details.get("kind") == "Remote"]
        bindables = [c for c in contexts if c.details.get("kind") == "Bindable"]
        values = [c for c in contexts if c.details.get("kind") == "ValueObject"]
        others = [c for c in contexts if c.details.get("kind") not in {"Remote", "Bindable", "ValueObject"}]

        lines = ["# Context objects (detailed)", ""]
        if remotes:
            lines += ["## Remotes", ""]
            for c in remotes:
                lines.append(f"- {c.full_path} ({c.class_name})")
            lines.append("")
        if bindables:
            lines += ["## Bindables", ""]
            for c in bindables:
                lines.append(f"- {c.full_path} ({c.class_name})")
            lines.append("")
        if values:
            lines += ["## ValueObjects", ""]
            for c in values:
                iv = c.details.get("initial_value", "")
                if iv != "":
                    lines.append(f"- {c.full_path} ({c.class_name}) = {iv}")
                else:
                    lines.append(f"- {c.full_path} ({c.class_name})")
            lines.append("")
        if others:
            lines += ["## Other context", ""]
            for c in others:
                lines.append(f"- {c.full_path} ({c.class_name})")
            lines.append("")

        safe_write_text(bundle_dir / "CONTEXT.txt", "\n".join(lines), encoding="utf-8")

    # ZIP
    zip_path = OUTPUT_DIR / f"{in_path.stem}_bundle.zip"
    try:
        if zip_path.exists():
            zip_path.unlink()
    except OSError as e:
        raise RuntimeError(f"Falha ao remover ZIP existente '{zip_path}': {e}") from e

    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in sorted((bundle_dir / "scripts").rglob("*")):
                if p.is_file():
                    z.write(p, arcname=str(p.relative_to(bundle_dir)))

            for fname in ["HIERARCHY.txt", "INDEX.csv", "ATTRIBUTES.csv", "ATTRIBUTES.txt"]:
                p = bundle_dir / fname
                if p.exists():
                    z.write(p, arcname=p.name)

            if include_context:
                p = bundle_dir / "CONTEXT.txt"
                if p.exists():
                    z.write(p, arcname=p.name)
    except (OSError, zipfile.BadZipFile) as e:
        raise RuntimeError(f"Falha ao criar ZIP '{zip_path}': {e}") from e

    return bundle_dir, zip_path, scripts


# ----------------- Main (UI) -----------------
def main() -> None:
    setup_logging()

    try:
        ensure_dirs()
    except Exception as e:
        LOG.error("❌ Falhou: %s", e)
        return

    hr()
    LOG.info("Roblox Bundle Extractor (bundle-only) ✅")
    hr()
    LOG.info(" input/: %s", INPUT_DIR.resolve())
    LOG.info(" output/: %s", OUTPUT_DIR.resolve())

    try:
        files = list_candidates()
    except Exception as e:
        LOG.error("❌ Falhou: %s", e)
        return

    if not files:
        LOG.info("Nenhum arquivo em input/. Coloque um .rbxmx/.xml/.txt lá e rode novamente.")
        return

    chosen = pick_file(files)
    if chosen is None:
        return

    include_context = ask_yes_no("Incluir CONTEXT.txt (detalhado)?", default_yes=True)

    try:
        bundle_dir, zip_path, scripts = build_bundle(chosen, include_context=include_context)
    except Exception as e:
        LOG.error("❌ Falhou: %s", e)
        return

    nonempty = sum(1 for s in scripts if s.source_len > 0)
    empty = len(scripts) - nonempty

    hr()
    LOG.info("✅ Concluído!")
    LOG.info("Arquivo: %s", chosen.name)
    LOG.info("Scripts detectados: %d", len(scripts))
    LOG.info(" - com Source não-vazio: %d", nonempty)
    LOG.info(" - com Source vazio: %d (pode ser export do Studio / scripts vazios / protegidos)", empty)
    LOG.info("Bundle dir: %s", bundle_dir)
    LOG.info("ZIP: %s", zip_path)
    hr()
    LOG.info("➡️ Envie o ZIP aqui no chat e diga: 'Analise o bundle'.")


if __name__ == "__main__":
    main()