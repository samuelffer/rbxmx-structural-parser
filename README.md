# rbxbundle

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

`rbxbundle` extracts Roblox `.rbxmx` and `.rbxlx` files into a compact bundle that is easier to inspect, version, and send to AI tools.

It keeps the useful parts of the project structure:
- scripts as `.lua` files
- hierarchy and index files
- attributes and optional context objects
- dependency graph outputs
- a `SUMMARY.md` designed to be the first file you share with an AI

## Demo

### CLI build

Replace with: ![CLI build demo](docs/demo-build.gif)

### Interactive mode

![Interactive mode demo](docs/demo-interactive.gif)

## Why use it

Roblox XML exports are noisy and token-heavy. Sending a raw `.rbxmx` or `.xml` file to an AI wastes context on markup instead of code and structure.

`rbxbundle` reduces that overhead by turning the export into a smaller, readable bundle focused on what matters, while trying to preserve the project context the AI actually needs.

## Installation

### Option A: Standalone `.exe`

Download `rbxbundle.exe` from the GitHub Releases page.

Recommended:
1. Create a folder such as `Documents/rbxbundle/`
2. Place `rbxbundle.exe` inside that folder
3. Open the executable to use the interactive interface

The standalone `.exe` uses its own folder as the working location, which makes it the easiest option for users who just want to download and use the tool.

### Option B: Install with Python

Requirements: Python `3.9+`.

```bash
git clone https://github.com/samuelffer/rbxbundle.git
cd rbxbundle
pip install .
```

Check the installed version:

```bash
rbxbundle --version
```

Default workspace paths for installed command-line usage:
- Installed command-line usage defaults to `~/Documents/rbxbundle/`
- Input files go in `~/Documents/rbxbundle/input/`
- Generated bundles go to `~/Documents/rbxbundle/output/`
- The standalone `.exe` keeps using the folder where the executable is located

## Quick start

Open the interactive mode:

```bash
rbxbundle
```

Build a bundle:

```bash
rbxbundle build MyModel.rbxmx
```

Inspect without writing files:

```bash
rbxbundle inspect MyModel.rbxmx
```

List supported files in a directory:

```bash
rbxbundle list --dir ./models
```

## Main commands

```text
rbxbundle
rbxbundle build <file> [--output DIR] [--no-context]
rbxbundle inspect <file>
rbxbundle list [--dir DIR]
rbxbundle --version
```

Notes:
- Running `rbxbundle` with no arguments opens the interactive mode.
- Use `build`, `inspect`, `list`, `--help`, or `--version` from a terminal for command-line usage.
- If you type an invalid command, `rbxbundle` now returns a command-line error instead of falling back to interactive mode.

Supported input extensions:
- `.rbxmx`
- `.rbxlx`
- `.xml`
- `.txt` with valid Roblox XML content

## Output

Each build generates `<name>_bundle/` plus a `.zip` with the same contents.

Core files:
- `SUMMARY.md`: high-level project overview
- `HIERARCHY.txt`: instance tree
- `INDEX.csv`: script inventory
- `scripts/`: extracted Lua files
- `ATTRIBUTES.txt` and `ATTRIBUTES.csv`: extracted attributes
- `DEPENDENCIES.json` and `EDGES.csv`: dependency graph outputs

Optional or conditional files:
- `CONTEXT.txt`: context objects when context export is enabled
- `DEPENDENCIES_ERROR.txt`: written when dependency analysis fails but bundle generation still completes

## Using with AI tools

1. Run `rbxbundle build YourModel.rbxmx`.
2. Upload the generated `.zip` or bundle folder contents.
3. Start with `SUMMARY.md`.
4. Reference scripts by their extracted path.

## Using as a library

```python
from pathlib import Path

from rbxbundle import create_bundle

bundle_dir, zip_path, scripts = create_bundle(
    Path("MyModel.rbxmx"),
    output_dir=Path("output"),
    include_context=True,
)
```

## Tests

```bash
python -m unittest discover tests -v
```

## License

MIT. See [LICENSE](LICENSE).
