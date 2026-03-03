from __future__ import annotations

import argparse
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
    CONTEXT_CLASSES,
    SCRIPT_CLASSES,
    ScriptRecord,
    create_bundle,
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
DEFAULT_INPUT_DIR = Path("input")
DEFAULT_OUTPUT_DIR = Path("output")
LOG = logging.getLogger("rbxbundle")

_ARGPARSE_COMMANDS = {"build", "inspect", "list", "help", "--help", "-h", "--version"}


def _resolve_config_path() -> Path:
    """Return the per-user config file path for the current OS."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "rbxbundle" / "rbxbundle.json"
        return Path.home() / ".rbxbundle" / "rbxbundle.json"
    return Path.home() / ".config" / "rbxbundle" / "rbxbundle.json"


_CONFIG_PATH = _resolve_config_path()
_DEFAULT_CONFIG: dict = {
    "startup_mode": "interactive",
    "input_dir": str(DEFAULT_INPUT_DIR),
    "output_dir": str(DEFAULT_OUTPUT_DIR),
}


def _load_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in _DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
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


def _banner() -> None:
    w = min(_term_width(), 72)
    print()
    _print_hr("=", BLU)
    title = "  RBX BUNDLE"
    ver = f"v{__version__}  "
    pad = w - len(title) - len(ver) - 2
    print(f"{clr(B + BLU, title)}{' ' * max(pad, 1)}{clr(GRY, ver)}")
    _print_hr("=", BLU)
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


def _inspect_file(in_path: Path) -> dict:
    """Return a dict with basic stats about a Roblox file (no output written)."""
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
        if cls in CONTEXT_CLASSES:
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
    try:
        bundle_dir, zip_path, scripts = create_bundle(
            in_path, output_dir=out_dir, include_context=include_context
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
        print(f"  {clr(B + PRP, '[4]')}  {clr(WHT, 'Settings')}      {clr(GRY, '- directories, startup mode, and more')}")
        print(f"  {clr(B + CYN, '[H]')}  {clr(WHT, 'Help')}          {clr(GRY, '- usage reference for CLI / argparse mode')}")
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
    _section("CLI / Argparse Reference")
    print()
    print(f"  {clr(GRY, 'When you pass arguments directly, rbxbundle runs in argparse mode')}")
    print(f"  {clr(GRY, 'and skips the interactive menus entirely.')}")
    print()

    rows = [
        ("COMMAND", "DESCRIPTION"),
        ("", ""),
        ("rbxbundle build <file>", "Generate a full bundle from a Roblox file"),
        ("  --output / -o  <dir>", "Output directory  (default: output/)"),
        ("  --no-context", "Skip CONTEXT.txt generation"),
        ("  --verbose / -v", "Enable debug logging"),
        ("", ""),
        ("rbxbundle inspect <file>", "Show stats without writing any output"),
        ("", ""),
        ("rbxbundle list", "List supported files in input/"),
        ("  --dir / -d  <dir>", "Directory to scan  (default: input/)"),
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

    _clear()
    _banner()
    _section(f"Build - Options for {clr(YLW, chosen.name)}")

    try:
        stats = _inspect_file(chosen)
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
        stats = _inspect_file(chosen)
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

        mode_label = clr(GRN, "interactive") if cfg["startup_mode"] == "interactive" else clr(YLW, "argparse (CLI)")
        _info(f"Startup mode  {mode_label}")
        _info(f"Input  dir    {clr(WHT, str(Path(cfg['input_dir']).resolve()))}")
        _info(f"Output dir    {clr(WHT, str(Path(cfg['output_dir']).resolve()))}")
        _info(f"Config file   {clr(WHT, str(_CONFIG_PATH))}")
        print()
        print(f"  {clr(B + CYN, '[1]')}  Toggle startup mode  {clr(GRY, '- interactive <-> argparse')}")
        print(f"  {clr(B + YLW, '[2]')}  Change input directory")
        print(f"  {clr(B + YLW, '[3]')}  Change output directory")
        print(f"  {clr(B + GRY, '[0]')}  Back")

        choice = _prompt("Choose:")

        if choice == "1":
            cfg["startup_mode"] = "argparse" if cfg["startup_mode"] == "interactive" else "interactive"
            _save_config(cfg)
            new_label = clr(GRN, "interactive") if cfg["startup_mode"] == "interactive" else clr(YLW, "argparse")
            _ok(f"Startup mode set to {new_label}")
            input("  Press Enter to continue.")
        elif choice == "2":
            raw = _prompt("New input directory path:")
            if raw:
                p = Path(raw)
                p.mkdir(parents=True, exist_ok=True)
                cfg["input_dir"] = str(p)
                _apply_config_defaults(cfg)
                _save_config(cfg)
                _ok(f"Input directory set to {p.resolve()}")
                input("  Press Enter to continue.")
        elif choice == "3":
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
    in_path = Path(args.file)

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
    in_path = Path(args.file)

    if not in_path.exists():
        print(f"  Error: file not found: {in_path}.", file=sys.stderr)
        return 1

    try:
        stats = _inspect_file(in_path)
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
              rbxbundle --mode interactive
              rbxbundle --version
        """),
    )

    parser.add_argument(
        "--mode",
        choices=["interactive", "argparse"],
        metavar="MODE",
        help="Set and save the default startup mode  (interactive | argparse).",
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

    return parser


def main() -> None:
    cfg = _load_config()
    _apply_config_defaults(cfg)

    args_passed = sys.argv[1:]
    first = args_passed[0] if args_passed else ""

    if "--mode" in args_passed:
        parser = _build_argparser()
        args = parser.parse_args()
        if args.mode:
            cfg["startup_mode"] = args.mode
            _save_config(cfg)
            label = clr(GRN, "interactive") if args.mode == "interactive" else clr(YLW, "argparse")
            print(f"\n  {clr(GRN, '[OK]')}  startup mode set to {label}")
            print(f"  {clr(GRY, f'saved to {_CONFIG_PATH}')}\n")
            if not args.command:
                sys.exit(0)
            level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
            setup_logging(level)
            dispatch = {"build": cmd_build, "inspect": cmd_inspect, "list": cmd_list}
            handler = dispatch.get(args.command)
            if handler:
                sys.exit(handler(args))
            sys.exit(0)

    use_argparse = first in _ARGPARSE_COMMANDS or (
        not args_passed and cfg["startup_mode"] == "argparse"
    )

    if use_argparse:
        parser = _build_argparser()
        args = parser.parse_args()
        level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
        setup_logging(level)

        dispatch = {
            "build": cmd_build,
            "inspect": cmd_inspect,
            "list": cmd_list,
        }
        handler = dispatch.get(args.command)
        if handler is None:
            parser.print_help()
            sys.exit(0)
        sys.exit(handler(args))

    setup_logging(logging.WARNING)
    _imode_main_menu()


if __name__ == "__main__":
    main()
