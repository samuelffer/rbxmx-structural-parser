#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET

# ----------------- Config -----------------
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
SUPPORTED_EXTS = {".rbxmx", ".xml", ".txt"}

SCRIPT_CLASSES = {"Script", "LocalScript", "ModuleScript"}
SOURCE_PROP_NAME = "Source"

# Alguns exports usam ProtectedString; outros podem variar. Mantemos heurística ampla.
SOURCE_TAG_NAMES = {"ProtectedString", "string", "SharedString"}

# Name normalmente vem em <string name="Name">...</string>
NAME_PROP_TAGS = {"string"}

CONTEXT_CLASSES = {
    "RemoteEvent", "RemoteFunction",
    "BindableEvent", "BindableFunction",
    "StringValue", "NumberValue", "BoolValue", "IntValue",
    "ObjectValue", "Folder", "Configuration",
}

INVALID_FS_CHARS = r'<>:"/\|?*\0'
INVALID_FS_RE = re.compile(f"[{re.escape(INVALID_FS_CHARS)}]")

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

# ----------------- Helpers -----------------
def ensure_dirs():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def hr():
    print("-" * 64)

def sanitize_filename(s: str) -> str:
    s = INVALID_FS_RE.sub("_", s)
    s = s.strip().strip(".")
    return s or "_"

def read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")

def local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag

def list_candidates() -> List[Path]:
    files = []
    for p in sorted(INPUT_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            files.append(p)
    return files

def pick_file(files: List[Path]) -> Optional[Path]:
    hr()
    print("Selecione um arquivo em ./input/")
    hr()
    for i, p in enumerate(files, start=1):
        print(f"[{i}] {p.name} ({p.stat().st_size/1024:.1f} KB)")
    print("[0] Sair")

    while True:
        s = input("\nNúmero: ").strip()
        if s.isdigit():
            n = int(s)
            if n == 0:
                return None
            if 1 <= n <= len(files):
                return files[n-1]
        print("Entrada inválida.")

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
        print("Responda com S ou N.")

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

def iter_top_level_items(roblox_root: ET.Element) -> List[ET.Element]:
    """
    Retorna todas as raízes relevantes para caminhar.
    Caso exista um DataModel no topo, caminhamos dentro dele.
    Caso contrário, caminhamos por todos os <Item> diretamente sob <roblox>.
    """
    # 1) Itens diretos sob <roblox>
    direct_items = [c for c in list(roblox_root) if local_tag(c.tag) == "Item"]

    # Se houver DataModel entre esses itens, use os filhos dele (mais completo e "oficial")
    for it in direct_items:
        if it.attrib.get("class") == "DataModel":
            return [c for c in list(it) if local_tag(c.tag) == "Item"]

    # Caso não haja DataModel, caminhar todos os items diretos
    return direct_items

def build_bundle(in_path: Path, include_context: bool) -> Tuple[Path, Path, List[ScriptRecord]]:
    xml_text = read_text(in_path)

    try:
        tree = ET.ElementTree(ET.fromstring(xml_text))
    except ET.ParseError as e:
        raise RuntimeError(f"Erro ao parsear XML: {e}")

    roblox_root = tree.getroot()
    if local_tag(roblox_root.tag).lower() != "roblox":
        # ainda assim pode funcionar, mas avisamos
        pass

    top_items = iter_top_level_items(roblox_root)
    if not top_items:
        raise RuntimeError("Não encontrei <Item> de topo no XML. Export pode estar incompleto/corrompido.")

    bundle_dir = OUTPUT_DIR / f"{in_path.stem}_bundle"
    scripts_dir = bundle_dir / "scripts"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    scripts: List[ScriptRecord] = []
    contexts: List[ContextRecord] = []
    hierarchy_lines: List[str] = []

    def walk(item: ET.Element, parent_path: str, depth: int):
        class_name = item.attrib.get("class", "UnknownClass")
        props = get_properties_node(item)

        name = get_name_from_properties(props)
        if name is None:
            # fallback: referent se não houver Name
            name = item.attrib.get("referent", "Unnamed")

        safe_name = sanitize_filename(name)
        full_path = f"{parent_path}/{safe_name}" if parent_path else safe_name

        indent = "  " * depth
        hierarchy_lines.append(f"{indent}- {safe_name} ({class_name})")

        if include_context and class_name in CONTEXT_CLASSES:
            contexts.append(ContextRecord(class_name=class_name, name=name, full_path=full_path))

        if class_name in SCRIPT_CLASSES:
            src = get_source_from_properties(props) or ""
            suffix = (
                ".server.lua" if class_name == "Script" else
                ".client.lua" if class_name == "LocalScript" else
                ".lua"
            )

            rel = Path(*(sanitize_filename(p) for p in full_path.split("/"))).with_suffix(suffix)
            out_file = scripts_dir / rel
            out_file.parent.mkdir(parents=True, exist_ok=True)

            header = (
                f"-- Extracted from RBXMX\n"
                f"-- Class: {class_name}\n"
                f"-- Name: {name}\n"
                f"-- Path: {full_path}\n\n"
            )
            out_file.write_text(header + src, encoding="utf-8")

            scripts.append(ScriptRecord(
                class_name=class_name,
                name=name,
                full_path=full_path,
                rel_file=str(Path("scripts") / rel),
                source_len=len(src),
            ))

        for child in item:
            if local_tag(child.tag) == "Item":
                walk(child, full_path, depth + 1)

    # ✅ Caminhar TODAS as raízes
    for it in top_items:
        walk(it, parent_path="", depth=0)

    # HIERARCHY
    (bundle_dir / "HIERARCHY.txt").write_text("\n".join(hierarchy_lines), encoding="utf-8")

    # INDEX
    with (bundle_dir / "INDEX.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["class", "name", "path", "file", "source_len"])
        for s in scripts:
            w.writerow([s.class_name, s.name, s.full_path, s.rel_file, s.source_len])

    # CONTEXT (optional)
    if include_context:
        lines = ["# Context objects (minimal)"]
        for c in contexts:
            lines.append(f"- {c.full_path} ({c.class_name})")
        (bundle_dir / "CONTEXT.txt").write_text("\n".join(lines), encoding="utf-8")

    # ZIP: scripts + hierarchy + index (+ context se existir)
    zip_path = OUTPUT_DIR / f"{in_path.stem}_bundle.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # scripts/
        for p in sorted((bundle_dir / "scripts").rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(bundle_dir)))

        # always
        for fname in ["HIERARCHY.txt", "INDEX.csv"]:
            p = bundle_dir / fname
            if p.exists():
                z.write(p, arcname=p.name)

        if include_context:
            p = bundle_dir / "CONTEXT.txt"
            if p.exists():
                z.write(p, arcname=p.name)

    return bundle_dir, zip_path, scripts

# ----------------- Main (UI) -----------------
def main():
    ensure_dirs()

    hr()
    print("Roblox Bundle Extractor (bundle-only) ✅")
    hr()
    print(f"📥 input/:  {INPUT_DIR.resolve()}")
    print(f"📤 output/: {OUTPUT_DIR.resolve()}")

    files = list_candidates()
    if not files:
        print("\nNenhum arquivo em input/. Coloque um .rbxmx/.xml/.txt lá e rode novamente.")
        return

    chosen = pick_file(files)
    if chosen is None:
        return

    include_context = ask_yes_no("Incluir CONTEXT.txt (remotes/values/folders úteis)?", default_yes=True)

    try:
        bundle_dir, zip_path, scripts = build_bundle(chosen, include_context=include_context)
    except Exception as e:
        print("\n❌ Falhou:", e)
        return

    # ✅ Prova local de que leu “tudo” do ponto de vista do extractor:
    # lista quantos scripts foram detectados e quantos tinham source > 0
    nonempty = sum(1 for s in scripts if s.source_len > 0)
    empty = len(scripts) - nonempty

    hr()
    print("✅ Concluído!")
    print(f"Arquivo: {chosen.name}")
    print(f"Scripts detectados: {len(scripts)}")
    print(f" - com Source não-vazio: {nonempty}")
    print(f" - com Source vazio:     {empty}  (pode ser export do Studio / scripts vazios / protegidos)")
    print(f"Bundle dir: {bundle_dir}")
    print(f"ZIP:       {zip_path}")
    hr()
    print("➡️ Envie o ZIP aqui no chat e diga: 'Analise o bundle'.")

if __name__ == "__main__":
    main()