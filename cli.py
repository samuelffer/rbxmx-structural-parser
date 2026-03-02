"""
rbxbundle CLI
=============

Two modes, one entry point:

  python cli.py              → launches the interactive TUI menu
  python cli.py build ...    → runs the argparse command directly (no menus)
  python cli.py inspect ...
  python cli.py list ...

The mode is chosen by whether the first argument is a known sub-command.
If it is → argparse mode.  If not (or no args) → interactive mode.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from rbxbundle.generator import CONTEXT_CLASSES, SCRIPT_CLASSES, create_bundle
from rbxbundle.parser import iter_top_level_items
from rbxbundle.utils import (
    ensure_dirs,
    read_text,
    setup_logging,
    strip_junk_before_roblox,
)

# ─── Constants ───────────────────────────────────────────────────────────────

SUPPORTED_EXTS    = {".rbxmx", ".rbxlx", ".xml", ".txt"}
DEFAULT_INPUT_DIR  = Path("input")
DEFAULT_OUTPUT_DIR = Path("output")
LOG = logging.getLogger("rbxbundle")

# Sub-commands that trigger argparse mode
_ARGPARSE_COMMANDS = {"build", "inspect", "list", "help", "--help", "-h"}

# ─── Terminal helpers ─────────────────────────────────────────────────────────

def _term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns

def _supports_color() -> bool:
    # Windows: check for ANSICON / WT_SESSION / Windows Terminal
    if os.name == "nt":
        return (
            "ANSICON" in os.environ
            or "WT_SESSION" in os.environ
            or os.environ.get("TERM_PROGRAM") == "vscode"
        )
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

# ANSI codes – fallback to empty strings if no color support
if _supports_color():
    R  = "\033[0m"        # reset
    B  = "\033[1m"        # bold
    DIM= "\033[2m"        # dim
    BLU= "\033[38;5;75m"  # blue
    GRN= "\033[38;5;83m"  # green
    YLW= "\033[38;5;221m" # yellow
    RED= "\033[38;5;210m" # red / orange
    PRP= "\033[38;5;183m" # purple
    CYN= "\033[38;5;87m"  # cyan
    GRY= "\033[38;5;240m" # dark grey
    WHT= "\033[38;5;252m" # white
else:
    R=B=DIM=BLU=GRN=YLW=RED=PRP=CYN=GRY=WHT = ""

def clr(color: str, text: str) -> str:
    return f"{color}{text}{R}"

def _hr(char: str = "─", color: str = GRY) -> str:
    return clr(color, char * min(_term_width(), 72))

def _print_hr(char: str = "─", color: str = GRY) -> None:
    print(_hr(char, color))

def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")

def _banner() -> None:
    w = min(_term_width(), 72)
    print()
    _print_hr("═", BLU)
    title = "  RBX BUNDLE"
    ver   = "v0.4.1  "
    pad   = w - len(title) - len(ver) - 2
    print(f"{clr(B+BLU, title)}{' ' * max(pad, 1)}{clr(GRY, ver)}")
    print(clr(GRY, "  .rbxmx Context Bundler"))
    _print_hr("═", BLU)
    print()

def _section(title: str) -> None:
    print(f"\n{clr(B+YLW, '  ' + title)}")
    _print_hr("─", GRY)

def _ok(msg: str)    -> None: print(f"  {clr(GRN, '✔')}  {msg}")
def _err(msg: str)   -> None: print(f"  {clr(RED, '✘')}  {clr(RED, msg)}")
def _info(msg: str)  -> None: print(f"  {clr(BLU, '·')}  {msg}")
def _warn(msg: str)  -> None: print(f"  {clr(YLW, '!')}  {clr(YLW, msg)}")
def _tip(msg: str)   -> None: print(f"  {clr(CYN, '→')}  {clr(DIM, msg)}")

def _prompt(msg: str) -> str:
    try:
        return input(f"\n  {clr(BLU, '›')} {msg} ").strip()
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

# ─── Shared logic (used by both modes) ───────────────────────────────────────

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
            if child.tag.split("}")[-1] == "Item":
                walk(child)

    for it in top_items:
        walk(it)

    return {
        "size_kb": in_path.stat().st_size / 1024.0,
        "instances": total_count,
        "scripts": script_count,
        "context": context_count,
    }

def _run_build(in_path: Path, out_dir: Path, include_context: bool) -> int:
    """Core build logic shared by interactive and argparse modes."""
    try:
        bundle_dir, zip_path, scripts = create_bundle(
            in_path, output_dir=out_dir, include_context=include_context
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return None, None, None, str(exc)

    nonempty = sum(1 for s in scripts if s.source_len > 0)
    return bundle_dir, zip_path, scripts, None   # err=None means success


# ══════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE MODE
# ══════════════════════════════════════════════════════════════════════════════

def _imode_main_menu() -> None:
    """Top-level interactive menu loop."""
    ensure_dirs(DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR)

    while True:
        _clear()
        _banner()

        _section("Main Menu")
        print(f"  {clr(B+GRN, '[1]')}  {clr(WHT, 'Build')}         {clr(GRY, '— process a file and generate the bundle')}")
        print(f"  {clr(B+BLU, '[2]')}  {clr(WHT, 'Inspect')}       {clr(GRY, '— peek inside a file without building')}")
        print(f"  {clr(B+YLW, '[3]')}  {clr(WHT, 'List files')}    {clr(GRY, '— show available files in input/')}")
        print(f"  {clr(B+PRP, '[4]')}  {clr(WHT, 'Settings')}      {clr(GRY, '— change input/output directories')}")
        print(f"  {clr(B+GRY, '[0]')}  {clr(GRY, 'Exit')}")
        print()
        _tip(f"Tip: run  {clr(WHT, 'python cli.py build <file>')}  to skip menus entirely.")

        choice = _prompt("Choose an option:")

        if choice == "1":
            _imode_build()
        elif choice == "2":
            _imode_inspect()
        elif choice == "3":
            _imode_list()
        elif choice == "4":
            _imode_settings()
        elif choice in ("0", "q", "exit", "quit", ""):
            _clear()
            print(f"\n  {clr(GRY, 'Goodbye.')}\n")
            sys.exit(0)
        else:
            _err("Invalid option. Press Enter to try again.")
            input()


# ─── interactive: BUILD ───────────────────────────────────────────────────────

def _imode_build() -> None:
    _clear()
    _banner()
    _section("Build — Select File")

    files = _scan_files(DEFAULT_INPUT_DIR)
    if not files:
        print()
        _warn(f"No supported files found in  {DEFAULT_INPUT_DIR}/")
        _tip("Put a .rbxmx or .rbxlx file there and try again.")
        _prompt("Press Enter to go back.")
        return

    for i, f in enumerate(files, 1):
        size_kb = f.stat().st_size / 1024.0
        name_col = clr(WHT, f.name)
        size_col = clr(GRY, f"({size_kb:.1f} KB)")
        print(f"  {clr(B+GRN, f'[{i}]')}  {name_col}  {size_col}")
    print(f"  {clr(B+GRY, '[0]')}  {clr(GRY, 'Back')}")

    idx = _pick_number("Select file:", 1, len(files))
    if idx is None:
        return

    chosen = files[idx - 1]

    # ── Options ──
    _clear()
    _banner()
    _section(f"Build — Options for  {clr(YLW, chosen.name)}")

    # Quick inspect preview
    try:
        stats = _inspect_file(chosen)
        _info(f"Size      " + clr(WHT, f"{stats['size_kb']:.1f} KB"))
        _info(f"Scripts   {clr(WHT, str(stats['scripts']))}")
        _info(f"Instances {clr(WHT, str(stats['instances']))}")
        print()
    except (ET.ParseError, OSError, ValueError) as exc:
        LOG.warning(
            "inspect_preview_failed file=%s section=inspect_preview data_type=xml error=%s",
            chosen,
            exc,
        )

    include_context = _yn("Include CONTEXT.txt? (RemoteEvents, ValueObjects…)", default_yes=True)
    out_dir = DEFAULT_OUTPUT_DIR

    # ── Build ──
    _clear()
    _banner()
    _section("Building…")
    _info(f"File   {clr(WHT, str(chosen))}")
    _info(f"Output {clr(WHT, str(out_dir.resolve()))}")
    print()

    bundle_dir, zip_path, scripts, err = _run_build(chosen, out_dir, include_context)

    if err:
        _err(f"Build failed: {err}")
        _prompt("Press Enter to go back.")
        return

    nonempty = sum(1 for s in scripts if s.source_len > 0)
    empty    = len(scripts) - nonempty

    _print_hr("─", GRN)
    _ok("Build complete!")
    _print_hr("─", GRN)
    print()
    _info(f"Scripts   {clr(WHT, str(len(scripts)))}  "
          f"({clr(GRN, str(nonempty))} with code, {clr(GRY, str(empty))} empty)")
    _info(f"Bundle    {clr(WHT, str(bundle_dir))}")
    _info(f"ZIP       {clr(BLU, str(zip_path))}")
    print()
    _tip("Upload the .zip to your AI tool and start with SUMMARY.md.")
    _prompt("Press Enter to return to the main menu.")


# ─── interactive: INSPECT ─────────────────────────────────────────────────────

def _imode_inspect() -> None:
    _clear()
    _banner()
    _section("Inspect — Select File")

    files = _scan_files(DEFAULT_INPUT_DIR)
    if not files:
        _warn(f"No supported files found in  {DEFAULT_INPUT_DIR}/")
        _prompt("Press Enter to go back.")
        return

    for i, f in enumerate(files, 1):
        size_kb = f.stat().st_size / 1024.0
        print(f"  {clr(B+BLU, f'[{i}]')}  {clr(WHT, f.name)}  {clr(GRY, f'({size_kb:.1f} KB)')}")
    print(f"  {clr(B+GRY, '[0]')}  {clr(GRY, 'Back')}")

    idx = _pick_number("Select file:", 1, len(files))
    if idx is None:
        return

    chosen = files[idx - 1]
    _clear()
    _banner()
    _section(f"Inspect — {chosen.name}")

    try:
        stats = _inspect_file(chosen)
        print()
        rows = [
            ("File",       chosen.name),
            ("Size",       f"{stats['size_kb']:.1f} KB"),
            ("Instances",  str(stats['instances'])),
            ("Scripts",    f"{stats['scripts']}  (Script / LocalScript / ModuleScript)"),
            ("Context",    f"{stats['context']}  (RemoteEvent, Folder, ValueObject…)"),
        ]
        label_w = max(len(r[0]) for r in rows) + 2
        for label, value in rows:
            pad = " " * (label_w - len(label))
            print(f"  {clr(GRY, label)}{pad}{clr(WHT, value)}")
        print()
    except (ET.ParseError, OSError, ValueError) as exc:
        _err(f"Could not inspect file: {exc}")

    _prompt("Press Enter to go back.")


# ─── interactive: LIST ────────────────────────────────────────────────────────

def _imode_list() -> None:
    _clear()
    _banner()
    _section(f"Files in  {DEFAULT_INPUT_DIR}/")

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
            print(f"  {clr(GRN, '•')}  {clr(WHT, f.name)}{pad}{clr(GRY, f'{size_kb:>8.1f} KB')}")
        print()
        _info(f"{len(files)} file(s) found.")

    print()
    _prompt("Press Enter to go back.")


# ─── interactive: SETTINGS ───────────────────────────────────────────────────

def _imode_settings() -> None:
    global DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR

    while True:
        _clear()
        _banner()
        _section("Settings")
        _info(f"Input  dir  {clr(WHT, str(DEFAULT_INPUT_DIR.resolve()))}")
        _info(f"Output dir  {clr(WHT, str(DEFAULT_OUTPUT_DIR.resolve()))}")
        print()
        print(f"  {clr(B+YLW, '[1]')}  Change input directory")
        print(f"  {clr(B+YLW, '[2]')}  Change output directory")
        print(f"  {clr(B+GRY, '[0]')}  Back")

        choice = _prompt("Choose:")
        if choice == "1":
            raw = _prompt("New input directory path:")
            if raw:
                p = Path(raw)
                p.mkdir(parents=True, exist_ok=True)
                DEFAULT_INPUT_DIR = p
                _ok(f"Input directory set to  {p.resolve()}")
                input("  Press Enter to continue.")
        elif choice == "2":
            raw = _prompt("New output directory path:")
            if raw:
                p = Path(raw)
                p.mkdir(parents=True, exist_ok=True)
                DEFAULT_OUTPUT_DIR = p
                _ok(f"Output directory set to  {p.resolve()}")
                input("  Press Enter to continue.")
        elif choice in ("0", "", "q"):
            return
        else:
            _err("Invalid option.")
            input()


# ══════════════════════════════════════════════════════════════════════════════
#  ARGPARSE MODE
# ══════════════════════════════════════════════════════════════════════════════

def cmd_build(args: argparse.Namespace) -> int:
    in_path = Path(args.file)

    if not in_path.exists():
        _err(f"File not found: {in_path}")
        return 1

    if in_path.suffix.lower() not in SUPPORTED_EXTS:
        _warn(f"Extension '{in_path.suffix}' is not officially supported. Proceeding anyway.")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    include_context = not args.no_context

    _print_hr("═", BLU)
    _ok(f"rbxbundle build")
    _info(f"Input   {clr(WHT, str(in_path.resolve()))}")
    _info(f"Output  {clr(WHT, str(out_dir.resolve()))}")
    _info(f"Context {clr(GRN, 'yes') if include_context else clr(GRY, 'no')}")
    _print_hr("═", BLU)

    bundle_dir, zip_path, scripts, err = _run_build(in_path, out_dir, include_context)

    if err:
        _err(f"Build failed: {err}")
        return 1

    nonempty = sum(1 for s in scripts if s.source_len > 0)
    empty    = len(scripts) - nonempty

    _print_hr("─", GRN)
    _ok("Done!")
    _info(f"Scripts   {len(scripts)}  ({nonempty} with code, {empty} empty)")
    _info(f"Bundle    {bundle_dir}")
    _info(f"ZIP       {clr(BLU, str(zip_path))}")
    _print_hr("─", GRN)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    in_path = Path(args.file)

    if not in_path.exists():
        _err(f"File not found: {in_path}")
        return 1

    try:
        stats = _inspect_file(in_path)
    except ET.ParseError as exc:
        _err(f"XML parse error: {exc}")
        return 1
    except (OSError, ValueError) as exc:
        _err(f"Failed to inspect: {exc}")
        return 1

    print()
    rows = [
        ("File",      in_path.name),
        ("Size",      f"{stats['size_kb']:.1f} KB"),
        ("Instances", str(stats['instances'])),
        ("Scripts",   f"{stats['scripts']}  (Script / LocalScript / ModuleScript)"),
        ("Context",   f"{stats['context']}  (RemoteEvent, Folder, ValueObject…)"),
    ]
    label_w = max(len(r[0]) for r in rows) + 2
    for label, value in rows:
        pad = " " * (label_w - len(label))
        print(f"  {clr(GRY, label)}{pad}{clr(WHT, value)}")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    in_dir = Path(args.dir)
    files  = _scan_files(in_dir)

    if not in_dir.exists():
        _err(f"Directory not found: {in_dir}")
        return 1

    if not files:
        _warn(f"No supported files found in  {in_dir}/")
        _tip(f"Supported extensions: {', '.join(sorted(SUPPORTED_EXTS))}")
        return 0

    print(f"\n  {clr(GRY, str(in_dir.resolve()))}\n")
    name_w = max(len(f.name) for f in files) + 2
    for f in files:
        size_kb = f.stat().st_size / 1024.0
        pad = " " * (name_w - len(f.name))
        print(f"  {clr(GRN, '•')}  {clr(WHT, f.name)}{pad}{clr(GRY, f'{size_kb:>8.1f} KB')}")
    print(f"\n  {clr(GRY, f'{len(files)} file(s) found.')}\n")
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbxbundle",
        description="rbxbundle — .rbxmx Context Bundler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python cli.py build MyModel.rbxmx
              python cli.py build MyModel.rbxmx --output ./out --no-context
              python cli.py inspect MyModel.rbxmx
              python cli.py list
              python cli.py list --dir ./models

            Run without arguments to open the interactive menu.
        """),
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging.")

    sub = parser.add_subparsers(dest="command")

    # build
    pb = sub.add_parser("build", help="Generate a bundle from a Roblox file.")
    pb.add_argument("file", help=".rbxmx / .rbxlx / .xml file to process.")
    pb.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT_DIR),
                    metavar="DIR", help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).")
    pb.add_argument("--no-context", action="store_true",
                    help="Skip CONTEXT.txt generation.")

    # inspect
    pi = sub.add_parser("inspect", help="Show stats for a file without building.")
    pi.add_argument("file", help=".rbxmx / .rbxlx / .xml file to inspect.")

    # list
    pl = sub.add_parser("list", help="List supported files in a directory.")
    pl.add_argument("--dir", "-d", default=str(DEFAULT_INPUT_DIR),
                    metavar="DIR", help=f"Directory to scan (default: {DEFAULT_INPUT_DIR}).")

    return parser


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT — mode router
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Decide mode BEFORE argparse sees argv, so argparse never interferes
    # with the interactive menu.
    args_passed = sys.argv[1:]
    first = args_passed[0] if args_passed else ""

    use_argparse = first in _ARGPARSE_COMMANDS

    if use_argparse:
        # ── Argparse mode ─────────────────────────────────────────────────
        parser = _build_argparser()
        args   = parser.parse_args()
        level  = logging.DEBUG if args.verbose else logging.INFO
        setup_logging(level)

        dispatch = {
            "build":   cmd_build,
            "inspect": cmd_inspect,
            "list":    cmd_list,
        }
        handler = dispatch.get(args.command)
        if handler is None:
            parser.print_help()
            sys.exit(0)
        sys.exit(handler(args))

    else:
        # ── Interactive mode ───────────────────────────────────────────────
        setup_logging(logging.WARNING)   # suppress INFO noise in TUI
        _imode_main_menu()


if __name__ == "__main__":
    main()