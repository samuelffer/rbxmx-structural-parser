from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from rbxbundle import __version__
from rbxbundle.generator import (
    SCRIPT_CLASSES,
    ScriptRecord,
    create_bundle,
    resolve_bundle_rules,
)
from rbxbundle.parser import iter_top_level_items
from rbxbundle.utils import (
    ensure_dirs,
    local_tag,
    read_text,
    setup_logging,
    strip_junk_before_roblox,
)

SUPPORTED_EXTS = {".rbxmx", ".rbxlx", ".xml", ".txt"}
LOG = logging.getLogger("rbxbundle")

_ARGPARSE_COMMANDS = {"build", "inspect", "list", "config", "help", "--help", "-h", "--version"}

_FOLDERID_DOCUMENTS = ctypes.c_char_p if os.name != "nt" else None


def _resolve_documents_dir() -> Path:
    """Return the real user Documents folder, including redirected OneDrive paths."""
    if os.name != "nt":
        return Path.home() / "Documents"

    try:
        from ctypes import POINTER, byref, c_wchar_p, windll
        from ctypes.wintypes import HRESULT
        from uuid import UUID

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def guid_from_string(value: str) -> GUID:
            u = UUID(value)
            data4 = (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:])
            return GUID(u.time_low, u.time_mid, u.time_hi_version, data4)

        documents_guid = guid_from_string("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
        path_ptr = c_wchar_p()
        shell32 = windll.shell32
        ole32 = windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, POINTER(c_wchar_p)]
        shell32.SHGetKnownFolderPath.restype = HRESULT

        result = shell32.SHGetKnownFolderPath(byref(documents_guid), 0, None, byref(path_ptr))
        if result == 0 and path_ptr.value:
            documents_dir = Path(path_ptr.value)
            ole32.CoTaskMemFree(path_ptr)
            return documents_dir
    except Exception:
        pass

    one_drive_docs = os.environ.get("ONEDRIVE")
    if one_drive_docs:
        candidate = Path(one_drive_docs) / "Documents"
        if candidate.exists():
            return candidate

    return Path.home() / "Documents"


def _resolve_default_workspace_root() -> Path:
    """Return the default workspace root for interactive/CLI file operations."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    documents_dir = _resolve_documents_dir()
    if documents_dir.exists():
        return documents_dir / "rbxbundle"
    return Path.home() / "rbxbundle"


_DEFAULT_WORKSPACE_ROOT = _resolve_default_workspace_root()
DEFAULT_INPUT_DIR = _DEFAULT_WORKSPACE_ROOT / "input"
DEFAULT_OUTPUT_DIR = _DEFAULT_WORKSPACE_ROOT / "output"


def _resolve_config_path() -> Path:
    """Return the per-user config file path for the current OS."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "rbxbundle" / "rbxbundle.json"
        return Path.home() / ".rbxbundle" / "rbxbundle.json"
    return Path.home() / ".config" / "rbxbundle" / "rbxbundle.json"


_CONFIG_PATH = _resolve_config_path()
_PROJECT_CONFIG_FILENAMES = ("rbxbundle.json", ".rbxbundle.json")
CONFIG_SCHEMA_VERSION = 1
_ROOT_CONFIG_KEYS = {
    "schema_version",
    "input_dir",
    "output_dir",
    "roblox_rules",
}
_RULE_SET_KEYS = {
    "context_classes",
    "value_object_classes",
    "min_hierarchy_classes",
    "min_hierarchy_folder_names",
}
_RULE_PREFIX_KEYS = {
    "server_only_prefixes",
    "client_only_prefixes",
    "primary_client_prefixes",
}
_DEFAULT_CONFIG: dict = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "input_dir": str(DEFAULT_INPUT_DIR),
    "output_dir": str(DEFAULT_OUTPUT_DIR),
}


class ConfigValidationError(ValueError):
    pass


def _normalize_string_list(
    value: object,
    *,
    source: Path,
    field_name: str,
    lower: bool = False,
) -> list[str] | None:
    if not isinstance(value, list):
        LOG.warning("Ignoring invalid config field %s in %s: expected a JSON array.", field_name, source)
        return None

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            LOG.warning("Ignoring invalid config field %s in %s: expected only strings.", field_name, source)
            return None
        text = item.strip()
        if not text:
            continue
        normalized.append(text.lower() if lower else text)

    return list(dict.fromkeys(normalized))


def _normalize_roblox_rules(raw: object, *, source: Path) -> dict:
    if not isinstance(raw, dict):
        LOG.warning("Ignoring invalid roblox_rules section in %s: expected a JSON object.", source)
        return {}

    normalized: dict = {}
    for key, value in raw.items():
        if key in _RULE_SET_KEYS:
            items = _normalize_string_list(
                value,
                source=source,
                field_name=f"roblox_rules.{key}",
                lower=key == "min_hierarchy_folder_names",
            )
            if items is not None:
                normalized[key] = items
        elif key in _RULE_PREFIX_KEYS:
            items = _normalize_string_list(
                value,
                source=source,
                field_name=f"roblox_rules.{key}",
            )
            if items is not None:
                normalized[key] = items
        else:
            LOG.warning("Ignoring unknown roblox_rules field %s in %s.", key, source)

    return normalized


def _normalize_schema_version(raw: object, *, source: Path) -> int | None:
    if raw is None:
        return CONFIG_SCHEMA_VERSION
    if not isinstance(raw, int):
        LOG.warning("Ignoring config at %s: schema_version must be an integer.", source)
        return None
    if raw != CONFIG_SCHEMA_VERSION:
        LOG.warning(
            "Ignoring config at %s: unsupported schema_version %s (supported: %s).",
            source,
            raw,
            CONFIG_SCHEMA_VERSION,
        )
        return None
    return raw


def _validate_config_data(data: object, *, source: Path) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        raise ConfigValidationError(f"{source}: expected a JSON object at the root.")

    warnings: list[str] = []
    schema_version = data.get("schema_version")
    if schema_version is None:
        warnings.append(
            f"{source}: missing schema_version; assuming legacy schema {CONFIG_SCHEMA_VERSION}."
        )
        schema_version = CONFIG_SCHEMA_VERSION

    if not isinstance(schema_version, int):
        raise ConfigValidationError(f"{source}: schema_version must be an integer.")

    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigValidationError(
            f"{source}: unsupported schema_version {schema_version} (supported: {CONFIG_SCHEMA_VERSION})."
        )

    cfg: dict = {"schema_version": schema_version}

    for key in data.keys():
        if key not in _ROOT_CONFIG_KEYS:
            warnings.append(f"{source}: unknown config field `{key}` will be ignored.")

    input_dir = data.get("input_dir")
    if input_dir is None or isinstance(input_dir, str):
        if input_dir is not None:
            cfg["input_dir"] = input_dir
    else:
        raise ConfigValidationError(f"{source}: input_dir must be a string.")

    output_dir = data.get("output_dir")
    if output_dir is None or isinstance(output_dir, str):
        if output_dir is not None:
            cfg["output_dir"] = output_dir
    else:
        raise ConfigValidationError(f"{source}: output_dir must be a string.")

    if "roblox_rules" in data:
        roblox_rules = data["roblox_rules"]
        if not isinstance(roblox_rules, dict):
            raise ConfigValidationError(f"{source}: roblox_rules must be a JSON object.")
        normalized_rules = _normalize_roblox_rules(roblox_rules, source=source)
        for key in roblox_rules.keys():
            if key not in _RULE_SET_KEYS and key not in _RULE_PREFIX_KEYS:
                warnings.append(f"{source}: unknown roblox_rules field `{key}` will be ignored.")
        cfg["roblox_rules"] = normalized_rules

    return cfg, warnings


def _load_config_file(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not load config at %s: %s", path, exc)
        return {}

    try:
        cfg, warnings = _validate_config_data(data, source=path)
    except ConfigValidationError as exc:
        LOG.warning("Ignoring config at %s: %s", path, exc)
        return {}

    for warning in warnings:
        LOG.warning(warning)

    return cfg


def _load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    cfg.update(_load_config_file(_CONFIG_PATH))
    return cfg


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        persisted = dict(cfg)
        persisted["schema_version"] = CONFIG_SCHEMA_VERSION
        _CONFIG_PATH.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not save config at %s: %s", _CONFIG_PATH, exc)


def _apply_config_defaults(cfg: dict) -> None:
    """Apply persisted directory defaults to the current process."""
    global DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR
    DEFAULT_INPUT_DIR = Path(cfg["input_dir"])
    DEFAULT_OUTPUT_DIR = Path(cfg["output_dir"])


def _term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _supports_color() -> bool:
    if os.name == "nt":
        return (
            "ANSICON" in os.environ
            or "WT_SESSION" in os.environ
            or os.environ.get("TERM_PROGRAM") == "vscode"
        )
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


if _supports_color():
    R = "\033[0m"
    B = "\033[1m"
    DIM = "\033[2m"
    BLU = "\033[38;5;75m"
    GRN = "\033[38;5;83m"
    YLW = "\033[38;5;221m"
    RED = "\033[38;5;210m"
    PRP = "\033[38;5;183m"
    CYN = "\033[38;5;87m"
    GRY = "\033[38;5;240m"
    WHT = "\033[38;5;252m"
else:
    R = B = DIM = BLU = GRN = YLW = RED = PRP = CYN = GRY = WHT = ""


def clr(color: str, text: str) -> str:
    return f"{color}{text}{R}"


def _hr(char: str = "-", color: str = GRY) -> str:
    return clr(color, char * min(_term_width(), 72))


def _print_hr(char: str = "-", color: str = GRY) -> None:
    print(_hr(char, color))


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _banner_lines() -> list[str]:
    width = min(_term_width(), 72)
    title = clr(B + BLU, "RBXBundle")
    version = clr(GRY, f"v{__version__}")
    status = clr(YLW, "In Development")

    plain_left = "RBXBundle"
    plain_right = f"v{__version__}"
    spacing = max(width - len(plain_left) - len(plain_right), 2)
    header = f"{title}{' ' * spacing}{version}"
    subheader = status
    return [header, subheader]


def _banner() -> None:
    print()
    for line in _banner_lines():
        print(f"  {line}")
    print()


def _section(title: str) -> None:
    print(f"\n{clr(B + YLW, '  ' + title)}")
    _print_hr("-", GRY)


def _ok(msg: str) -> None:
    print(f"  {clr(GRN, '[OK]')}  {msg}")


def _err(msg: str) -> None:
    print(f"  {clr(RED, '[X]')}  {clr(RED, msg)}")


def _info(msg: str) -> None:
    print(f"  {clr(BLU, '-')}  {msg}")


def _warn(msg: str) -> None:
    print(f"  {clr(YLW, '!')}  {clr(YLW, msg)}")


def _tip(msg: str) -> None:
    print(f"  {clr(CYN, '->')}  {clr(DIM, msg)}")


def _prompt(msg: str) -> str:
    try:
        return input(f"\n  {clr(BLU, '>')} {msg} ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return ""


def _yn(question: str, default_yes: bool = True) -> bool:
    hint = clr(GRN, "Y") + clr(GRY, "/n") if default_yes else clr(GRY, "y/") + clr(GRN, "N")
    while True:
        ans = _prompt(f"{question} [{hint}]").lower()
        if ans == "":
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        _err("Please type y or n.")


def _pick_number(prompt: str, lo: int, hi: int) -> int | None:
    """Ask for a number in [lo..hi]. Returns None on 0/cancel/empty."""
    while True:
        raw = _prompt(prompt)
        if raw == "" or raw == "0":
            return None
        if raw.isdigit():
            n = int(raw)
            if lo <= n <= hi:
                return n
        _err(f"Enter a number between {lo} and {hi} (or 0 to go back).")


def _scan_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )


def _resolve_cli_input_path(raw_path: str) -> Path:
    """Resolve a CLI input file, falling back to the default input directory."""
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate

    if not candidate.is_absolute():
        fallback = DEFAULT_INPUT_DIR / candidate
        if fallback.exists():
            return fallback

    return candidate


def _find_project_config_path(start_path: Path) -> Path | None:
    directory = start_path if start_path.is_dir() else start_path.parent
    for current in (directory, *directory.parents):
        for filename in _PROJECT_CONFIG_FILENAMES:
            candidate = current / filename
            if candidate.exists():
                return candidate
    return None


def _find_config_in_cwd() -> Path | None:
    cwd = Path.cwd()
    for filename in _PROJECT_CONFIG_FILENAMES:
        candidate = cwd / filename
        if candidate.exists():
            return candidate
    return None


def _resolve_config_validation_target(raw_path: str | None) -> Path | None:
    if raw_path:
        return Path(raw_path)
    return _find_config_in_cwd()


def _bundle_rules_for_path(in_path: Path) -> dict | None:
    rules = dict(_load_config().get("roblox_rules") or {})
    project_path = _find_project_config_path(in_path.parent)
    if project_path is not None and project_path != _CONFIG_PATH:
        project_cfg = _load_config_file(project_path)
        rules.update(project_cfg.get("roblox_rules") or {})
    return rules or None


def _inspect_file(in_path: Path, rules: dict | None = None) -> dict:
    """Return a dict with basic stats about a Roblox file (no output written)."""
    bundle_rules = resolve_bundle_rules(rules)
    xml_text = strip_junk_before_roblox(read_text(in_path))
    root = ET.fromstring(xml_text)
    top_items = iter_top_level_items(root)

    script_count = context_count = total_count = 0

    def walk(item: ET.Element) -> None:
        nonlocal script_count, context_count, total_count
        cls = item.attrib.get("class", "")
        total_count += 1
        if cls in SCRIPT_CLASSES:
            script_count += 1
        if cls in bundle_rules.context_classes:
            context_count += 1
        for child in item:
            if local_tag(child.tag) == "Item":
                walk(child)

    for it in top_items:
        walk(it)

    return {
        "size_kb": in_path.stat().st_size / 1024.0,
        "instances": total_count,
        "scripts": script_count,
        "context": context_count,
    }


def _run_build(
    in_path: Path,
    out_dir: Path,
    include_context: bool,
) -> tuple[Path | None, Path | None, list[ScriptRecord] | None, str | None]:
    """Core build logic shared by interactive and argparse modes."""
    rules = _bundle_rules_for_path(in_path)
    try:
        bundle_dir, zip_path, scripts = create_bundle(
            in_path,
            output_dir=out_dir,
            include_context=include_context,
            rules=rules,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return None, None, None, str(exc)

    return bundle_dir, zip_path, scripts, None


def _imode_main_menu() -> None:
    """Top-level interactive menu loop."""
    cfg = _load_config()
    _apply_config_defaults(cfg)
    ensure_dirs(DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR)

    while True:
        _clear()
        _banner()

        _section("Main Menu")
        print(f"  {clr(B + GRN, '[1]')}  {clr(WHT, 'Build')}         {clr(GRY, '- process a file and generate the bundle')}")
        print(f"  {clr(B + BLU, '[2]')}  {clr(WHT, 'Inspect')}       {clr(GRY, '- peek inside a file without building')}")
        print(f"  {clr(B + YLW, '[3]')}  {clr(WHT, 'List files')}    {clr(GRY, '- show available files in input/')}")
        print(f"  {clr(B + PRP, '[4]')}  {clr(WHT, 'Settings')}      {clr(GRY, '- input/output directories')}")
        print(f"  {clr(B + CYN, '[H]')}  {clr(WHT, 'Help')}          {clr(GRY, '- usage reference for command-line mode')}")
        print(f"  {clr(B + GRY, '[0]')}  {clr(GRY, 'Exit')}")
        print()

        choice = _prompt("Choose an option:")

        if choice == "1":
            _imode_build()
        elif choice == "2":
            _imode_inspect()
        elif choice == "3":
            _imode_list()
        elif choice == "4":
            cfg = _imode_settings(cfg)
            _apply_config_defaults(cfg)
        elif choice.lower() == "h":
            _imode_help()
        elif choice in ("0", "q", "exit", "quit", ""):
            _clear()
            print(f"\n  {clr(GRY, 'Goodbye.')}\n")
            sys.exit(0)
        else:
            _err("Invalid option. Press Enter to try again.")
            input()


def _imode_help() -> None:
    _clear()
    _banner()
    _section("Command-line Reference")
    print()
    print(f"  {clr(GRY, 'Opening rbxbundle with no arguments starts the interactive mode.')}")
    print(f"  {clr(GRY, 'Passing commands directly runs the command-line interface instead.')}")
    print(f"  {clr(GRY, f'Default workspace: {_DEFAULT_WORKSPACE_ROOT}')}")
    print()

    rows = [
        ("COMMAND", "DESCRIPTION"),
        ("", ""),
        ("rbxbundle build <file>", "Generate a full bundle from a Roblox file"),
        ("  --output / -o  <dir>", f"Output directory  (default: {DEFAULT_OUTPUT_DIR})"),
        ("  --no-context", "Skip CONTEXT.txt generation"),
        ("  --verbose / -v", "Enable debug logging"),
        ("", ""),
        ("rbxbundle inspect <file>", "Show stats without writing any output"),
        ("", ""),
        ("rbxbundle list", "List supported files in the default input directory"),
        ("  --dir / -d  <dir>", f"Directory to scan  (default: {DEFAULT_INPUT_DIR})"),
        ("", ""),
        ("rbxbundle config validate [file]", "Validate rbxbundle.json schema and fields"),
        ("", ""),
        ("rbxbundle --help", "Show this reference and exit"),
    ]

    cmd_w = max(len(r[0]) for r in rows) + 3
    for cmd, desc in rows:
        if cmd == "COMMAND":
            print(f"  {clr(B + GRY, cmd.ljust(cmd_w))}{clr(B + GRY, desc)}")
            continue
        if cmd == "":
            print()
            continue
        color = YLW if not cmd.startswith("  ") else GRY
        print(f"  {clr(color, cmd.ljust(cmd_w))}{clr(WHT, desc)}")

    print()
    _section("Examples")
    examples = [
        "rbxbundle build MyPlane.rbxmx",
        "rbxbundle build MyPlane.rbxmx --output ./bundles --no-context",
        "rbxbundle inspect MyModel.rbxmx",
        "rbxbundle list --dir ./models",
        "rbxbundle config validate",
    ]
    for ex in examples:
        print(f"  {clr(CYN, '->')}  {clr(WHT, ex)}")

    print()
    _section("Supported file extensions")
    print(f"  {clr(WHT, '.rbxmx')}  {clr(GRY, 'Roblox model file')}")
    print(f"  {clr(WHT, '.rbxlx')}  {clr(GRY, 'Roblox place file')}")
    print(f"  {clr(WHT, '.xml')}    {clr(GRY, 'Generic XML')}")
    print(f"  {clr(WHT, '.txt')}    {clr(GRY, 'Plain text (must contain valid Roblox XML)')}")
    print()
    _prompt("Press Enter to go back.")


def _imode_build() -> None:
    _clear()
    _banner()
    _section("Build - Select File")

    files = _scan_files(DEFAULT_INPUT_DIR)
    if not files:
        print()
        _warn(f"No supported files found in {DEFAULT_INPUT_DIR}/")
        _tip("Put a .rbxmx or .rbxlx file there and try again.")
        _prompt("Press Enter to go back.")
        return

    for i, f in enumerate(files, 1):
        size_kb = f.stat().st_size / 1024.0
        print(f"  {clr(B + GRN, f'[{i}]')}  {clr(WHT, f.name)}  {clr(GRY, f'({size_kb:.1f} KB)')}")
    print(f"  {clr(B + GRY, '[0]')}  {clr(GRY, 'Back')}")

    idx = _pick_number("Select file:", 1, len(files))
    if idx is None:
        return

    chosen = files[idx - 1]
    rules = _bundle_rules_for_path(chosen)

    _clear()
    _banner()
    _section(f"Build - Options for {clr(YLW, chosen.name)}")

    try:
        stats = _inspect_file(chosen, rules=rules)
        _info("Size      " + clr(WHT, f"{stats['size_kb']:.1f} KB"))
        _info(f"Scripts   {clr(WHT, str(stats['scripts']))}")
        _info(f"Instances {clr(WHT, str(stats['instances']))}")
        print()
    except (ET.ParseError, OSError, ValueError) as exc:
        LOG.warning(
            "inspect_preview_failed file=%s section=inspect_preview data_type=xml error=%s",
            chosen,
            exc,
        )

    include_context = _yn("Include CONTEXT.txt? (RemoteEvents, ValueObjects...)", default_yes=True)
    out_dir = DEFAULT_OUTPUT_DIR

    _clear()
    _banner()
    _section("Building...")
    _info(f"File   {clr(WHT, str(chosen))}")
    _info(f"Output {clr(WHT, str(out_dir.resolve()))}")
    print()

    bundle_dir, zip_path, scripts, err = _run_build(chosen, out_dir, include_context)

    if err:
        _err(f"Build failed: {err}")
        _prompt("Press Enter to go back.")
        return

    if scripts is None or bundle_dir is None or zip_path is None:
        _err("Build failed: unexpected empty result.")
        _prompt("Press Enter to go back.")
        return

    nonempty = sum(1 for s in scripts if s.source_len > 0)
    empty = len(scripts) - nonempty

    _print_hr("-", GRN)
    _ok("Build complete!")
    _print_hr("-", GRN)
    print()
    _info(f"Scripts   {clr(WHT, str(len(scripts)))}  ({clr(GRN, str(nonempty))} with code, {clr(GRY, str(empty))} empty)")
    _info(f"Bundle    {clr(WHT, str(bundle_dir))}")
    _info(f"ZIP       {clr(BLU, str(zip_path))}")
    print()
    _tip("Upload the .zip to your AI tool and start with SUMMARY.md.")
    _prompt("Press Enter to return to the main menu.")


def _imode_inspect() -> None:
    _clear()
    _banner()
    _section("Inspect - Select File")

    files = _scan_files(DEFAULT_INPUT_DIR)
    if not files:
        _warn(f"No supported files found in {DEFAULT_INPUT_DIR}/")
        _prompt("Press Enter to go back.")
        return

    for i, f in enumerate(files, 1):
        size_kb = f.stat().st_size / 1024.0
        print(f"  {clr(B + BLU, f'[{i}]')}  {clr(WHT, f.name)}  {clr(GRY, f'({size_kb:.1f} KB)')}")
    print(f"  {clr(B + GRY, '[0]')}  {clr(GRY, 'Back')}")

    idx = _pick_number("Select file:", 1, len(files))
    if idx is None:
        return

    chosen = files[idx - 1]
    _clear()
    _banner()
    _section(f"Inspect - {chosen.name}")

    try:
        stats = _inspect_file(chosen, rules=_bundle_rules_for_path(chosen))
        print()
        rows = [
            ("File", chosen.name),
            ("Size", f"{stats['size_kb']:.1f} KB"),
            ("Instances", str(stats["instances"])),
            ("Scripts", f"{stats['scripts']}  (Script / LocalScript / ModuleScript)"),
            ("Context", f"{stats['context']}  (RemoteEvent, Folder, ValueObject...)"),
        ]
        label_w = max(len(r[0]) for r in rows) + 2
        for label, value in rows:
            pad = " " * (label_w - len(label))
            print(f"  {clr(GRY, label)}{pad}{clr(WHT, value)}")
        print()
    except (ET.ParseError, OSError, ValueError) as exc:
        _err(f"Could not inspect file: {exc}")

    _prompt("Press Enter to go back.")


def _imode_list() -> None:
    _clear()
    _banner()
    _section(f"Files in {DEFAULT_INPUT_DIR}/")

    files = _scan_files(DEFAULT_INPUT_DIR)
    if not files:
        print()
        _warn("No supported files found.")
        _tip(f"Supported extensions: {', '.join(sorted(SUPPORTED_EXTS))}")
    else:
        print()
        name_w = max(len(f.name) for f in files) + 2
        for f in files:
            size_kb = f.stat().st_size / 1024.0
            pad = " " * (name_w - len(f.name))
            print(f"  {clr(GRN, '*')}  {clr(WHT, f.name)}{pad}{clr(GRY, f'{size_kb:>8.1f} KB')}")
        print()
        _info(f"{len(files)} file(s) found.")

    print()
    _prompt("Press Enter to go back.")


def _imode_settings(cfg: dict) -> dict:
    while True:
        _clear()
        _banner()
        _section("Settings")
        _info(f"Input  dir    {clr(WHT, str(Path(cfg['input_dir']).resolve()))}")
        _info(f"Output dir    {clr(WHT, str(Path(cfg['output_dir']).resolve()))}")
        _info(f"Config file   {clr(WHT, str(_CONFIG_PATH))}")
        print()
        print(f"  {clr(B + YLW, '[1]')}  Change input directory")
        print(f"  {clr(B + YLW, '[2]')}  Change output directory")
        print(f"  {clr(B + GRY, '[0]')}  Back")

        choice = _prompt("Choose:")

        if choice == "1":
            raw = _prompt("New input directory path:")
            if raw:
                p = Path(raw)
                p.mkdir(parents=True, exist_ok=True)
                cfg["input_dir"] = str(p)
                _apply_config_defaults(cfg)
                _save_config(cfg)
                _ok(f"Input directory set to {p.resolve()}")
                input("  Press Enter to continue.")
        elif choice == "2":
            raw = _prompt("New output directory path:")
            if raw:
                p = Path(raw)
                p.mkdir(parents=True, exist_ok=True)
                cfg["output_dir"] = str(p)
                _apply_config_defaults(cfg)
                _save_config(cfg)
                _ok(f"Output directory set to {p.resolve()}")
                input("  Press Enter to continue.")
        elif choice in ("0", "", "q"):
            return cfg
        else:
            _err("Invalid option.")
            input()


def cmd_build(args: argparse.Namespace) -> int:
    in_path = _resolve_cli_input_path(args.file)

    if not in_path.exists():
        print(f"  Error: file not found: {in_path}.", file=sys.stderr)
        return 1

    if in_path.suffix.lower() not in SUPPORTED_EXTS:
        print(f"  Warning: extension '{in_path.suffix}' is not officially supported; proceeding anyway.")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    include_context = not args.no_context

    print(f"\n  {clr(B + BLU, 'rbxbundle build')}")
    print(f"  {'input':<10} {clr(WHT, str(in_path))}")
    print(f"  {'output':<10} {clr(WHT, str(out_dir))}")
    print(f"  {'context':<10} {clr(GRN, 'yes') if include_context else clr(GRY, 'no')}")
    print()

    bundle_dir, zip_path, scripts, err = _run_build(in_path, out_dir, include_context)

    if err:
        print(f"  {clr(RED, 'Error:')} {err}", file=sys.stderr)
        return 1

    if scripts is None or bundle_dir is None or zip_path is None:
        print(f"  {clr(RED, 'Error:')} unexpected empty result.", file=sys.stderr)
        return 1

    nonempty = sum(1 for s in scripts if s.source_len > 0)
    empty = len(scripts) - nonempty

    print(f"  {clr(GRN, '[OK]')}  Done.")
    print(f"  {'scripts':<10} {len(scripts)}  {clr(GRY, f'({nonempty} with code, {empty} empty)')}")
    print(f"  {'bundle':<10} {clr(WHT, str(bundle_dir))}")
    print(f"  {'zip':<10} {clr(BLU, str(zip_path))}")
    print()
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    in_path = _resolve_cli_input_path(args.file)

    if not in_path.exists():
        print(f"  Error: file not found: {in_path}.", file=sys.stderr)
        return 1

    try:
        stats = _inspect_file(in_path, rules=_bundle_rules_for_path(in_path))
    except ET.ParseError as exc:
        print(f"  Error: XML parse error: {exc}.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"  Error: {exc}.", file=sys.stderr)
        return 1

    print(f"\n  {clr(B + BLU, 'rbxbundle inspect')}")
    rows = [
        ("file", in_path.name),
        ("size", f"{stats['size_kb']:.1f} KB"),
        ("instances", str(stats["instances"])),
        ("scripts", f"{stats['scripts']}  {clr(GRY, '(Script / LocalScript / ModuleScript)')}"),
        ("context", f"{stats['context']}  {clr(GRY, '(RemoteEvent, Folder, ValueObject...)')}"),
    ]
    for label, value in rows:
        print(f"  {clr(GRY, f'{label:<12}')}{value}")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    in_dir = Path(args.dir)

    if not in_dir.exists():
        print(f"  Error: directory not found: {in_dir}.", file=sys.stderr)
        return 1

    files = _scan_files(in_dir)

    print(f"\n  {clr(B + BLU, 'rbxbundle list')}  {clr(GRY, str(in_dir))}")
    print()

    if not files:
        print(f"  {clr(GRY, 'No supported files found.')}")
        print(f"  {clr(GRY, 'supported: ' + ', '.join(sorted(SUPPORTED_EXTS)))}")
    else:
        name_w = max(len(f.name) for f in files) + 2
        for f in files:
            size_kb = f.stat().st_size / 1024.0
            pad = " " * (name_w - len(f.name))
            print(f"  {clr(WHT, f.name)}{pad}{clr(GRY, f'{size_kb:.1f} KB')}")
        print(f"\n  {clr(GRY, f'{len(files)} file(s)')}")
    print()
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    target = _resolve_config_validation_target(getattr(args, "file", None))
    if target is None:
        print("  Error: no rbxbundle.json found in the current directory.", file=sys.stderr)
        return 1

    if not target.exists():
        print(f"  Error: config file not found: {target}.", file=sys.stderr)
        return 1

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  Error: could not parse config: {exc}.", file=sys.stderr)
        return 1

    try:
        normalized, warnings = _validate_config_data(data, source=target)
    except ConfigValidationError as exc:
        print(f"\n  {clr(RED, 'Config invalid')}")
        print(f"  {exc}")
        print()
        return 1

    print(f"\n  {clr(B + BLU, 'rbxbundle config validate')}")
    print(f"  {'file':<10} {clr(WHT, str(target))}")
    print(f"  {'schema':<10} {normalized['schema_version']}")
    if "input_dir" in normalized:
        print(f"  {'input_dir':<10} {clr(WHT, normalized['input_dir'])}")
    if "output_dir" in normalized:
        print(f"  {'output_dir':<10} {clr(WHT, normalized['output_dir'])}")
    if normalized.get("roblox_rules"):
        print(f"  {'rules':<10} {len(normalized['roblox_rules'])} override set(s)")
    else:
        print(f"  {'rules':<10} 0 override set(s)")
    print()

    if warnings:
        print(f"  {clr(YLW, '[WARN]')}  Config is valid with warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print(f"  {clr(GRN, '[OK]')}  Config is valid.")
    print()
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbxbundle",
        description="rbxbundle - bundle extractor for Roblox .rbxmx / .rbxlx files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              rbxbundle build MyModel.rbxmx
              rbxbundle build MyModel.rbxmx --output ./out --no-context
              rbxbundle inspect MyModel.rbxmx
              rbxbundle list
              rbxbundle list --dir ./models
              rbxbundle config validate
              rbxbundle config validate ./rbxbundle.json
              rbxbundle --version
        """),
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", "-v", action="store_true", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command")

    pb = sub.add_parser("build", help="Generate a bundle from a Roblox file.")
    pb.add_argument("file", help=".rbxmx / .rbxlx / .xml / .txt file to process.")
    pb.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_OUTPUT_DIR),
        metavar="DIR",
        help=f"Output directory  (default: {DEFAULT_OUTPUT_DIR}).",
    )
    pb.add_argument("--no-context", action="store_true", help="Skip CONTEXT.txt generation.")

    pi = sub.add_parser("inspect", help="Show stats for a file without writing output.")
    pi.add_argument("file", help=".rbxmx / .rbxlx / .xml / .txt file to inspect.")

    pl = sub.add_parser("list", help="List supported files in a directory.")
    pl.add_argument(
        "--dir",
        "-d",
        default=str(DEFAULT_INPUT_DIR),
        metavar="DIR",
        help=f"Directory to scan  (default: {DEFAULT_INPUT_DIR}).",
    )

    pc = sub.add_parser("config", help="Configuration helpers.")
    config_sub = pc.add_subparsers(dest="config_command")
    pcv = config_sub.add_parser("validate", help="Validate a rbxbundle.json file.")
    pcv.add_argument(
        "file",
        nargs="?",
        help="Config file to validate. Defaults to ./rbxbundle.json or ./.rbxbundle.json.",
    )

    return parser


def _should_use_argparse(args_passed: list[str]) -> bool:
    """Return True whenever any CLI arguments are passed."""
    return bool(args_passed)


def main() -> None:
    cfg = _load_config()
    _apply_config_defaults(cfg)
    ensure_dirs(DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR)

    args_passed = sys.argv[1:]
    use_argparse = _should_use_argparse(args_passed)

    if use_argparse:
        parser = _build_argparser()
        args = parser.parse_args()
        level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
        setup_logging(level)

        dispatch = {
            "build": cmd_build,
            "inspect": cmd_inspect,
            "list": cmd_list,
            "config": cmd_config_validate,
        }
        handler = dispatch.get(args.command)
        if handler is None:
            parser.print_help()
            sys.exit(0)
        if args.command == "config" and getattr(args, "config_command", None) != "validate":
            parser.print_help()
            sys.exit(0)
        sys.exit(handler(args))

    setup_logging(logging.WARNING)
    _imode_main_menu()


if __name__ == "__main__":
    main()
