# rbxbundle

Roblox RBXMX/XML bundle extractor.

Extracts `.rbxmx`, `.xml`, or `.txt` files containing Roblox model XML and generates a structured bundle with hierarchy, script index, and separated script files.

---

## Requirements

* Python 3.9+
* No external dependencies

---

## Usage

Place your file inside:

```
input/
```

Run:

```bash
python rbxmx_bundle.py
```

Select the file and choose whether to generate the context file.

---

## Output

Generated inside:

```
output/
```

Bundle contents:

* `HIERARCHY.txt` → full model hierarchy
* `INDEX.csv` → detected scripts table
* `scripts/` → extracted scripts
* `CONTEXT.txt` → optional structural objects list
* `<name>_bundle.zip` → packaged bundle

---

## Features

* Detects DataModel root automatically
* Traverses all `<Item>` nodes
* Extracts Script, LocalScript, ModuleScript
* Sanitizes file paths
* Generates ZIP bundle automatically

---

## Limitations


* No require resolution
* Invalid XML may fail

---

## License

MIT