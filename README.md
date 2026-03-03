# rbxbundle
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Works with Claude](https://img.shields.io/badge/works%20with-Claude%20%7C%20ChatGPT-blueviolet)

`rbxbundle` parses Roblox model files (`.rbxmx`, `.rbxlx`) and produces a clean, structured bundle ready to be uploaded to any AI tool — saving tokens and giving the AI accurate context about your project.

---

## Why does this exist?

Roblox `.rbxmx` files are verbose XML. Sending one raw to an AI burns most of the context window on tags, not code. `rbxbundle` strips the noise and outputs only what matters: scripts, hierarchy, remotes, attributes, and a dependency graph.

---

## Installation

**Requirements:** Python 3.9+ — no external dependencies.

```bash
git clone https://github.com/samuelffer/rbxbundle.git
cd rbxbundle
pip install -e .
```

After installation the `rbxbundle` command is available globally.

Alternatively, run without installing:

```bash
python cli.py
```

---

## Quick start

```bash
rbxbundle build MyModel.rbxmx
```

Output lands in `output/MyModel_bundle/` and a ready-to-upload `.zip`.

---

## CLI Reference

### `rbxbundle build <file>`

Parse a Roblox file and generate the full bundle.

```
rbxbundle build <file> [--output DIR] [--no-context] [--verbose]
```

| Flag | Default | Description |
|------|---------|-------------|
| `<file>` | *(required)* | Path to `.rbxmx`, `.rbxlx`, `.xml`, or `.txt` file |
| `--output`, `-o` | `output/` | Directory where the bundle is written |
| `--no-context` | off | Skip `CONTEXT.txt` (RemoteEvents, ValueObjects, …) |
| `--verbose`, `-v` | off | Enable debug-level logging |

**Examples:**

```bash
# Basic usage
rbxbundle build MyPlane.rbxmx

# Custom output directory
rbxbundle build MyPlane.rbxmx --output ./bundles

# Skip context objects (smaller output)
rbxbundle build MyPlane.rbxmx --no-context

# Debug mode
rbxbundle build MyPlane.rbxmx --verbose
```

---

### `rbxbundle inspect <file>`

Print a quick summary of a file **without writing any output**.

```
rbxbundle inspect <file>
```

```bash
rbxbundle inspect MyModel.rbxmx
```

```
File     : MyModel.rbxmx
Size     : 142.3 KB
Instances: 831
Scripts  : 12  (Script / LocalScript / ModuleScript)
Context  : 24  (RemoteEvent, Folder, ValueObject, …)
```

Useful to check what a file contains before running a full build.

---

### `rbxbundle list [--dir DIR]`

List all supported files in a directory.

```
rbxbundle list [--dir DIR]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--dir`, `-d` | `input/` | Directory to scan |

```bash
rbxbundle list
rbxbundle list --dir ./models
```

---

### Interactive mode (no arguments)

Running `rbxbundle` with no arguments starts an interactive prompt — pick a file from `input/` and answer a yes/no for context inclusion.

```bash
rbxbundle
```

---

## Output files

Every build produces a folder (`<name>_bundle/`) and a `.zip` of the same contents.

| File | Description |
|------|-------------|
| `SUMMARY.md` | Human-readable project overview — **start here when prompting an AI** |
| `HIERARCHY.txt` | Full instance tree with class names |
| `INDEX.csv` | Table of all scripts: path, type, source length |
| `scripts/` | Every script as a `.lua` file with a header comment |
| `CONTEXT.txt` | RemoteEvents, RemoteFunctions, BindableEvents, ValueObjects |
| `ATTRIBUTES.txt` | All custom attributes in readable form |
| `ATTRIBUTES.csv` | Same data in CSV for programmatic use |
| `DEPENDENCIES.json` | Dependency graph: nodes (scripts) + edges (require calls) |
| `EDGES.csv` | Flat edge list from the dependency graph |

---

## How to use the bundle with an AI

1. Run `rbxbundle build YourModel.rbxmx`
2. Open the `.zip` (or the folder) in your file explorer
3. Upload the `.zip` to your AI tool **or** paste individual files
4. Start your prompt with `SUMMARY.md` — it gives the AI an instant overview
5. Reference scripts by their path (e.g. `StarterCharacterScripts/Plane`)

**Tip:** If the AI loses context in a long session, re-paste `SUMMARY.md` and `HIERARCHY.txt`.

---

## Using as a Python library

```python
from rbxbundle import create_bundle, generate_summary
from pathlib import Path

bundle_dir, zip_path, scripts = create_bundle(
    Path("MyModel.rbxmx"),
    output_dir=Path("output"),
    include_context=True,
)

print(f"Extracted {len(scripts)} scripts → {zip_path}")
```

Other public exports:

```python
from rbxbundle import (
    # Core
    create_bundle, generate_summary,
    # Records
    ScriptRecord, ContextRecord, AttributeRecord,
    # Parser primitives
    get_name, get_source, iter_top_level_items, parse_attributes,
    # Dependency graph
    build_dependency_graph, find_require_calls, Node, ScriptInfo,
    # Utilities
    read_text, sanitize_filename, strip_junk_before_roblox,
)
```

---

## Running tests

```bash
python -m unittest discover tests/ -v
```

No external packages required — stdlib only.

---

## Supported file types

| Extension | Description |
|-----------|-------------|
| `.rbxmx` | Roblox model file (XML) |
| `.rbxlx` | Roblox place file (XML) |
| `.xml` | Generic XML |
| `.txt` | Plain text (must contain valid Roblox XML) |

---

## License

MIT — see [LICENSE](LICENSE).
