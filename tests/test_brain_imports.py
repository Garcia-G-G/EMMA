"""BRAIN-1 rule 1: core/brain imports nothing from the platform — transitively.

Walks every module under core/brain, follows each in-repo import to its file and
on through THAT file's module-level imports, and fails if the closure reaches
the platform: ``actions``, ``core.platform._mac``, PyObjC frameworks,
``subprocess``, or ``tools`` (whose registry imports every tool, and with them
``actions``). Inside core/brain every import counts, function-level included;
outside it, module-level imports are what an ``import`` actually executes.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "core" / "brain"

FORBIDDEN_PREFIXES = (
    "actions",
    "core.platform._mac",
    "tools",
    "subprocess",
    "objc",
    "AppKit",
    "Foundation",
    "Quartz",
    "Cocoa",
    "ApplicationServices",
    "PyObjCTools",
    "ScriptingBridge",
)


def _forbidden(mod: str) -> bool:
    return any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def _module_file(mod: str) -> Path | None:
    base = ROOT.joinpath(*mod.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.exists():
            return cand
    return None


def _imports(path: Path, module_level_only: bool) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = ".".join(path.relative_to(ROOT).with_suffix("").parts)
    if path.name == "__init__.py":
        pkg = pkg.rsplit(".", 1)[0] if "." in pkg else pkg
    nodes = tree.body if module_level_only else list(ast.walk(tree))
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.If) and module_level_only:
            # `if TYPE_CHECKING:` blocks never execute at runtime.
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                continue
            nodes.extend(node.body + node.orelse)
            continue
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = pkg.rsplit(".", node.level - (path.name == "__init__.py"))[0]
                base = f"{parent}.{node.module}" if node.module else parent
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names)
    return found


def _closure(starts: list[Path]) -> dict[str, list[str]]:
    """module -> the import chain that reached it, for every module reachable.

    Files inside core/brain contribute every import (function-level too); any
    other file contributes its module-level imports.
    """
    seen: dict[str, list[str]] = {}
    stack: list[tuple[Path, list[str]]] = [
        (p, [".".join(p.relative_to(ROOT).with_suffix("").parts)]) for p in starts
    ]
    visited: set[Path] = set()
    while stack:
        path, chain = stack.pop()
        if path in visited:
            continue
        visited.add(path)
        for mod in _imports(path, module_level_only=not path.is_relative_to(BRAIN)):
            seen.setdefault(mod, [*chain, mod])
            f = _module_file(mod)
            if f is not None and f not in visited:
                stack.append((f, [*chain, mod]))
    return seen


def _violations(starts: list[Path]) -> dict[str, list[str]]:
    return {m: chain for m, chain in _closure(starts).items() if _forbidden(m)}


def test_brain_package_exists() -> None:
    assert (BRAIN / "__init__.py").exists()


def test_brain_never_reaches_the_platform() -> None:
    bad = _violations(sorted(BRAIN.rglob("*.py")))
    assert not bad, "core/brain reaches the platform:\n" + "\n".join(
        " -> ".join(chain) for chain in bad.values()
    )


def test_the_guard_follows_imports_transitively() -> None:
    """It must fail on a module that is clean itself but imports the platform two hops down.

    core.orchestrator has no platform import of its own; it imports
    core.conversation, which imports actions. A first-level-only check passes it.
    """
    orch = _module_file("core.orchestrator")
    assert orch is not None
    direct = _imports(orch, module_level_only=True)
    assert not any(_forbidden(m) for m in direct), "premise: no direct platform import"
    assert _violations([orch]), "the transitive walk missed core.orchestrator -> ... -> platform"
