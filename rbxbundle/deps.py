"""Dependency graph extraction for Roblox/Luau scripts.

This module is intentionally dependency-free (no external parser).
It uses a robust-ish lexer pass to ignore comments/strings, then
extracts require(...) calls with balanced parentheses.

Resolution is best-effort for common Roblox patterns:
- require(script.Parent.Mod)
- require(script:WaitForChild("Mod"))
- require(game:GetService("ReplicatedStorage").X.Y)
- local RS = game:GetService("ReplicatedStorage") ; require(RS.X)

Dynamic requires remain unresolved but still recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import re


# ---------------------------
# Models
# ---------------------------


@dataclass(frozen=True)
class Node:
    """A node in the extracted instance tree."""

    class_name: str
    name: str
    safe_name: str
    full_path: str  # slash-separated bundle path, e.g. ReplicatedStorage/Flight/World_CONFIG
    parent_path: str


@dataclass(frozen=True)
class ScriptInfo:
    """Minimal script info needed for dependency analysis."""

    class_name: str
    name: str
    full_path: str  # slash-separated
    source: str


@dataclass(frozen=True)
class RequireEdge:
    src: str
    dst: Optional[str]
    kind: str  # instance|servicePath|assetId|dynamic|unknown
    expr: str
    line: Optional[int]
    confidence: float


# ---------------------------
# Public API
# ---------------------------


def build_dependency_graph(
    scripts: Iterable[ScriptInfo],
    nodes: Dict[str, Node],
) -> Tuple[List[dict], List[dict]]:
    """Return (nodes_json, edges_json).

    nodes_json: list of dicts for scripts (graph nodes)
    edges_json: list of dicts for require dependencies
    """

    node_out: List[dict] = []
    edge_out: List[dict] = []

    script_nodes: Dict[str, ScriptInfo] = {s.full_path: s for s in scripts}

    for s in scripts:
        node_out.append(
            {
                "id": s.full_path,
                "class": s.class_name,
                "name": s.name,
                "path": s.full_path,
            }
        )

    # Build lookup tables for resolution
    child_by_parent_and_name: Dict[Tuple[str, str], str] = {}
    for p, n in nodes.items():
        # both original name and safe name should resolve
        child_by_parent_and_name[(n.parent_path, n.name)] = n.full_path
        child_by_parent_and_name[(n.parent_path, n.safe_name)] = n.full_path

    # Fast lookups for heuristic resolution
    extracted_modules_by_name: Dict[str, List[str]] = {}
    for si in scripts:
        if si.class_name == "ModuleScript":
            extracted_modules_by_name.setdefault(si.name, []).append(si.full_path)

    for s in scripts:
        aliases = _collect_service_aliases(s.source)
        string_fallbacks = _collect_string_fallbacks(s.source)
        var_folder_hints = _collect_var_folder_hints(s.source)

        for call in find_require_calls(s.source):
            expr = call.arg.strip()
            resolved, kind, conf = resolve_require_expr(
                expr,
                src_script_path=s.full_path,
                nodes=nodes,
                child_by_parent_and_name=child_by_parent_and_name,
                service_aliases=aliases,
            )

            # Heuristic: resolve dynamic WaitForChild(var) when var has a literal fallback.
            # Example:
            #   local configName = model:GetAttribute("CurrentConfig") or "Trainer_CONFIG"
            #   require(cfgFolder:WaitForChild(configName))
            # We cannot know the runtime attribute value, but we can add a best-effort
            # edge to the fallback module when it exists in the extracted bundle.
            if resolved is None:
                h_resolved, h_kind, h_conf = _heuristic_resolve_require(
                    expr,
                    service_aliases=aliases,
                    string_fallbacks=string_fallbacks,
                    var_folder_hints=var_folder_hints,
                    extracted_modules_by_name=extracted_modules_by_name,
                    nodes=nodes,
                    child_by_parent_and_name=child_by_parent_and_name,
                )
                if h_resolved is not None:
                    resolved, kind, conf = h_resolved, h_kind, h_conf

            # Only point to scripts we actually have, unless you want to
            # include non-script nodes too. For now, if resolved points to a
            # non-script, still keep it (AI can interpret), but mark confidence.
            if resolved is not None:
                # if resolved is an instance path, it might be a Folder etc.
                # keep it anyway.
                pass

            edge_out.append(
                {
                    "from": s.full_path,
                    "to": resolved,
                    "kind": kind,
                    "expr": expr,
                    "confidence": conf,
                    "loc": {"line": call.line, "col": None} if call.line else None,
                }
            )

    return node_out, edge_out


# ---------------------------
# Require call extraction
# ---------------------------


@dataclass(frozen=True)
class RequireCall:
    arg: str
    line: Optional[int]


def find_require_calls(source: str) -> List[RequireCall]:
    """Extract require(<arg>) calls from Luau/Lua source.

    This ignores strings and comments, supports nested parentheses,
    and returns the raw argument string.
    """

    masked, idx_to_line = _mask_lua_strings_and_comments(source)

    calls: List[RequireCall] = []
    i = 0
    n = len(masked)

    while True:
        j = masked.find("require", i)
        if j < 0:
            break

        # ensure it's an identifier boundary: not foo.requireX
        if j > 0 and (masked[j - 1].isalnum() or masked[j - 1] == "_"):
            i = j + 7
            continue

        k = j + 7
        while k < n and masked[k].isspace():
            k += 1

        if k >= n or masked[k] != "(":
            i = j + 7
            continue

        end = _find_matching_paren(masked, k)
        if end is None:
            i = j + 7
            continue

        arg = source[k + 1 : end]
        line = idx_to_line.get(j)
        calls.append(RequireCall(arg=arg, line=line))
        i = end + 1

    return calls


def _find_matching_paren(text: str, open_idx: int) -> Optional[int]:
    if open_idx >= len(text) or text[open_idx] != "(":
        return None
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


# ---------------------------
# Masking (comments/strings)
# ---------------------------


def _mask_lua_strings_and_comments(src: str) -> Tuple[str, Dict[int, int]]:
    """Return (masked_src, index_to_line).

    masked_src replaces content of strings/comments with spaces, preserving length.
    index_to_line maps a subset of indices to line numbers (1-based), for locations.
    """

    out = list(src)
    idx_to_line: Dict[int, int] = {}

    i = 0
    line = 1
    n = len(src)

    def mark_to_space(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        idx_to_line[i] = line

        if c == "\n":
            line += 1
            i += 1
            continue

        # line comment -- ...
        if c == "-" and i + 1 < n and src[i + 1] == "-":
            # block comment --[[ ... ]] or --[=[ ... ]=]
            if i + 3 < n and src[i + 2] == "[":
                eq = _long_bracket_eq_count(src, i + 2)
                if eq is not None:
                    end = _find_long_bracket_end(src, i + 2, eq)
                    if end is None:
                        mark_to_space(i, n)
                        break
                    mark_to_space(i, end)
                    i = end
                    continue

            # normal line comment
            j = src.find("\n", i)
            if j == -1:
                mark_to_space(i, n)
                break
            mark_to_space(i, j)
            i = j
            continue

        # long string [[...]] or [=[...]=]
        if c == "[":
            eq = _long_bracket_eq_count(src, i)
            if eq is not None:
                end = _find_long_bracket_end(src, i, eq)
                if end is None:
                    mark_to_space(i, n)
                    break
                mark_to_space(i, end)
                i = end
                continue

        # quoted string "..." or '...'
        if c in ("\"", "'"):
            q = c
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q:
                    j += 1
                    break
                if src[j] == "\n":
                    # unterminated string, still treat as ended
                    break
                j += 1
            mark_to_space(i, min(j, n))
            i = j
            continue

        i += 1

    return "".join(out), idx_to_line


def _long_bracket_eq_count(src: str, i: int) -> Optional[int]:
    """If src[i:] begins a long bracket opener [=*[ return eq_count else None."""
    if i >= len(src) or src[i] != "[":
        return None
    j = i + 1
    while j < len(src) and src[j] == "=":
        j += 1
    if j < len(src) and src[j] == "[":
        return j - (i + 1)
    return None


def _find_long_bracket_end(src: str, i: int, eq: int) -> Optional[int]:
    """Find end index (exclusive) for long bracket starting at i."""
    opener = "[" + ("=" * eq) + "["
    closer = "]" + ("=" * eq) + "]"
    assert src.startswith(opener, i)
    j = src.find(closer, i + len(opener))
    if j == -1:
        return None
    return j + len(closer)


# ---------------------------
# Alias collection
# ---------------------------


_SERVICE_ALIAS_RE = re.compile(
    r"\blocal\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*game\s*:\s*GetService\s*\(\s*(?P<q>\"|')(?P<svc>[^\"']+)(?P=q)\s*\)",
    re.MULTILINE,
)


def _collect_service_aliases(source: str) -> Dict[str, str]:
    """Return mapping var -> ServiceName for patterns like:

    local RS = game:GetService("ReplicatedStorage")
    """

    # We must keep string literals (service names) intact, but ignore comments.
    masked = _mask_lua_comments_only(source)
    aliases: Dict[str, str] = {}
    for m in _SERVICE_ALIAS_RE.finditer(masked):
        aliases[m.group("var")] = m.group("svc")
    return aliases


def _mask_lua_comments_only(src: str) -> str:
    """Replace comment text with spaces (preserving newlines), keep strings intact."""

    out = list(src)
    i = 0
    n = len(src)

    def mark_to_space(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]

        # line comment -- ...
        if c == "-" and i + 1 < n and src[i + 1] == "-":
            # block comment --[[ ... ]] or --[=[ ... ]=]
            if i + 3 < n and src[i + 2] == "[":
                eq = _long_bracket_eq_count(src, i + 2)
                if eq is not None:
                    end = _find_long_bracket_end(src, i + 2, eq)
                    if end is None:
                        mark_to_space(i, n)
                        break
                    mark_to_space(i, end)
                    i = end
                    continue

            # normal line comment
            j = src.find("\n", i)
            if j == -1:
                mark_to_space(i, n)
                break
            mark_to_space(i, j)
            i = j
            continue

        i += 1

    return "".join(out)


_STRING_FALLBACK_RE = re.compile(
    r"\blocal\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^\n;]*?\bor\b\s*(?P<q>\"|')(?P<lit>[^\"']+)(?P=q)",
    re.MULTILINE,
)


def _collect_string_fallbacks(source: str) -> Dict[str, str]:
    """Collect variables that have a literal fallback string.

    Example:
      local configName = model:GetAttribute("CurrentConfig") or "Trainer_CONFIG"
    """

    masked = _mask_lua_comments_only(source)
    out: Dict[str, str] = {}
    for m in _STRING_FALLBACK_RE.finditer(masked):
        out[m.group("var")] = m.group("lit")
    return out


_VAR_FOLDER_HINT_RE = re.compile(
    r"\blocal\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<base>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(WaitForChild|FindFirstChild)\s*\(\s*(?P<q>\"|')(?P<child>[^\"']+)(?P=q)\s*\)",
    re.MULTILINE,
)


def _collect_var_folder_hints(source: str) -> Dict[str, str]:
    """Collect simple hints about vars bound to a folder-like child.

    Example:
      local cfgFolder = model:WaitForChild("Config")
    -> {"cfgFolder": "Config"}

    Used only to prefer modules under a folder with that name.
    """

    masked = _mask_lua_comments_only(source)
    out: Dict[str, str] = {}
    for m in _VAR_FOLDER_HINT_RE.finditer(masked):
        out[m.group("var")] = m.group("child")
    return out



# ---------------------------
# Resolution
# ---------------------------


# Simple tokenization for dot/colon calls we support.
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|(?P<dot>\.)|(?P<colon>:)|(?P<lparen>\()|(?P<rparen>\))|(?P<comma>,)|(?P<string>\"([^\\\"\n]|\\.)*\"|'([^\\'\n]|\\.)*'))",
    re.DOTALL,
)


def resolve_require_expr(
    expr: str,
    *,
    src_script_path: str,
    nodes: Dict[str, Node],
    child_by_parent_and_name: Dict[Tuple[str, str], str],
    service_aliases: Dict[str, str],
) -> Tuple[Optional[str], str, float]:
    """Resolve a require() argument to a bundle instance path if possible."""

    expr = expr.strip()

    # AssetId require (number literal)
    if re.fullmatch(r"\d+", expr):
        return None, "assetId", 0.0

    # direct game:GetService(...) root
    svc = _try_parse_getservice(expr)
    if svc is not None:
        # expr might be exactly game:GetService("X") (rare to require directly)
        # treat as unresolved service object
        return svc, "servicePath", 0.2

    # Parse supported chain: Root (script|game:GetService|alias) then .Name / :WaitForChild("Name") / .Parent
    chain = _parse_chain(expr)
    if chain is None:
        # Could be variable or complex expression.
        return None, "dynamic", 0.0

    root_kind, root_value, steps = chain

    # Determine starting path
    cur: Optional[str]
    kind = "instance"
    conf = 1.0

    if root_kind == "script":
        cur = src_script_path
    elif root_kind == "service":
        cur = root_value
        kind = "servicePath"
    elif root_kind == "alias":
        svc_name = service_aliases.get(root_value)
        if not svc_name:
            return None, "dynamic", 0.0
        cur = svc_name
        kind = "servicePath"
    else:
        return None, "unknown", 0.0

    # Apply steps
    for st_kind, st_val in steps:
        if cur is None:
            return None, kind, 0.0

        if st_kind == "parent":
            node = nodes.get(cur)
            if node is None:
                # service roots might not be present in nodes; try string split fallback
                if "/" in cur:
                    cur = cur.rsplit("/", 1)[0]
                    conf = min(conf, 0.7)
                else:
                    return None, kind, 0.0
            else:
                cur = node.parent_path
            continue

        if st_kind == "child":
            nxt = child_by_parent_and_name.get((cur, st_val))
            if nxt is None:
                return None, kind, 0.0
            cur = nxt
            continue

        return None, kind, 0.0

    return cur, kind, conf


_DYNAMIC_WAITFORCHILD_RE = re.compile(
    r"^(?P<root>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(WaitForChild|FindFirstChild)\s*\(\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$"
)


def _heuristic_resolve_require(
    expr: str,
    *,
    service_aliases: Dict[str, str],
    string_fallbacks: Dict[str, str],
    var_folder_hints: Dict[str, str],
    extracted_modules_by_name: Dict[str, List[str]],
    nodes: Dict[str, Node],
    child_by_parent_and_name: Dict[Tuple[str, str], str],
) -> Tuple[Optional[str], str, float]:
    """Best-effort resolver for some dynamic patterns.

    Currently supports:
      require(cfgFolder:WaitForChild(configName))
    when `configName` has a literal fallback (e.g. `or "Trainer_CONFIG"`).

    The returned edge is heuristic (confidence < 1.0).
    """

    expr = expr.strip()
    m = _DYNAMIC_WAITFORCHILD_RE.match(expr)
    if not m:
        return None, "dynamic", 0.0

    root_var = m.group("root")
    var_name = m.group("var")

    fallback = string_fallbacks.get(var_name)
    if not fallback:
        return None, "dynamic", 0.0

    # If fallback corresponds to an extracted ModuleScript by name, pick best candidate.
    candidates = extracted_modules_by_name.get(fallback, [])
    if not candidates:
        return None, "dynamic", 0.0

    # Prefer candidate whose parent folder name matches a known hint (e.g. cfgFolder -> "Config").
    folder_hint = var_folder_hints.get(root_var)
    if folder_hint and len(candidates) > 1:
        filtered: List[str] = []
        for c in candidates:
            parent_path = nodes.get(c).parent_path if nodes.get(c) else c.rsplit("/", 1)[0] if "/" in c else ""
            parent_node = nodes.get(parent_path)
            if parent_node and parent_node.name == folder_hint:
                filtered.append(c)
        if filtered:
            candidates = filtered

    # If still ambiguous, take the first but lower confidence.
    chosen = candidates[0]
    conf = 0.55 if len(candidates) == 1 else 0.35
    return chosen, "heuristicFallback", conf


def _try_parse_getservice(expr: str) -> Optional[str]:
    m = re.fullmatch(
        r"game\s*:\s*GetService\s*\(\s*(\"|')([^\"']+)(\"|')\s*\)",
        expr.strip(),
    )
    if not m:
        return None
    return m.group(2)


def _parse_chain(expr: str) -> Optional[Tuple[str, str, List[Tuple[str, str]]]]:
    """Parse subset of Luau expressions for instance navigation.

    Returns: (root_kind, root_value, steps)

    root_kind:
      - "script" (root_value unused)
      - "service" root_value = ServiceName
      - "alias" root_value = identifier

    steps: list of (kind, value)
      - ("parent", "")
      - ("child", name)

    Supported forms:
      - script.Parent.Mod
      - script:WaitForChild("Mod").X
      - game:GetService("ReplicatedStorage").A.B
      - Alias:WaitForChild("X")

    Anything else -> None.
    """

    tokens = list(_iter_tokens(expr))
    if not tokens:
        return None

    pos = 0

    def peek() -> Optional[Tuple[str, str]]:
        return tokens[pos] if pos < len(tokens) else None

    def consume(expected_type: str) -> Optional[str]:
        nonlocal pos
        t = peek()
        if t and t[0] == expected_type:
            pos += 1
            return t[1]
        return None

    # Root
    t = peek()
    if not t:
        return None

    root_kind: str
    root_value: str = ""

    if t[0] == "ident" and t[1] == "script":
        pos += 1
        root_kind = "script"
    elif t[0] == "ident" and t[1] == "game":
        # parse game:GetService("X")
        pos += 1
        if not consume("colon"):
            return None
        ident = consume("ident")
        if ident != "GetService":
            return None
        if not consume("lparen"):
            return None
        s = consume("string")
        if s is None:
            return None
        svc = _unquote(s)
        if not consume("rparen"):
            return None
        root_kind = "service"
        root_value = svc
    elif t[0] == "ident":
        # alias root (e.g. ReplicatedStorage)
        pos += 1
        root_kind = "alias"
        root_value = t[1]
    else:
        return None

    steps: List[Tuple[str, str]] = []

    while pos < len(tokens):
        if consume("dot"):
            ident = consume("ident")
            if ident is None:
                return None
            if ident == "Parent":
                steps.append(("parent", ""))
            else:
                steps.append(("child", ident))
            continue

        if consume("colon"):
            method = consume("ident")
            if method not in ("WaitForChild", "FindFirstChild"):
                return None
            if not consume("lparen"):
                return None
            s = consume("string")
            if s is None:
                return None
            name = _unquote(s)
            if not consume("rparen"):
                return None
            steps.append(("child", name))
            continue

        # If anything else appears, we give up.
        return None

    return root_kind, root_value, steps


def _iter_tokens(expr: str):
    i = 0
    n = len(expr)
    while i < n:
        m = _TOKEN_RE.match(expr, i)
        if not m:
            # unknown token => abort
            return
        i = m.end()
        if m.group("ident"):
            yield ("ident", m.group("ident"))
        elif m.group("dot"):
            yield ("dot", ".")
        elif m.group("colon"):
            yield ("colon", ":")
        elif m.group("lparen"):
            yield ("lparen", "(")
        elif m.group("rparen"):
            yield ("rparen", ")")
        elif m.group("string"):
            yield ("string", m.group("string"))
        elif m.group("comma"):
            yield ("comma", ",")
        else:
            # whitespace-only match
            continue


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("\"", "'"):
        return bytes(s[1:-1], "utf-8").decode("unicode_escape")
    return s
