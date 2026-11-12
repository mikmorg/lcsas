"""Import boundaries between the packages under ``src/lcsas`` (#700).

1. No module imports an underscore-prefixed name, or a name from an
   underscore-prefixed module, that lives in ANOTHER package.  A private
   name can change without notice; a name other packages need gets a
   public name in its home module.
2. Code outside ``restore/`` (and the ``cli``/``meta`` front ends) reaches
   into the restore package only through the tier-3 reader
   (``restore.restic_fallback``) and the generator of its on-disc copy
   (``restore.standalone_builder``).  Those two are leaves: they import
   nothing from LCSAS outside their own pure-Python helpers, so the burn,
   staging and packs code never depends on the rest of the restore code.
   The burn and stage checks use the tier-3 reader on purpose: they must
   prove the mirror decodes with the very reader the discs carry.
3. ``binpack`` does not import ``iso``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lcsas"

# Front ends that may import anything.
_FRONT_ENDS = {"cli", "meta", "__main__"}

# The restore modules other packages may import, and the LCSAS modules each
# of them may import in turn (the leaf check).
_RESTORE_LEAVES: dict[str, set[str]] = {
    "lcsas.restore.restic_fallback": {
        "lcsas.restore._aes_pure", "lcsas.restore._zstd_pure",
    },
    "lcsas.restore.standalone_builder": set(),
    "lcsas.restore._aes_pure": set(),
    "lcsas.restore._zstd_pure": set(),
}
_RESTORE_PUBLIC = {
    "lcsas.restore.restic_fallback", "lcsas.restore.standalone_builder",
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package(module: str) -> str:
    """Top-level package under ``lcsas`` (``lcsas.burn.x`` -> ``burn``)."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ""


def _imports(path: Path) -> Iterator[tuple[int, str, str | None]]:
    """Every LCSAS import in *path*, at any depth: (line, module, name)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module and node.module.split(".")[0] == "lcsas":
                for alias in node.names:
                    yield node.lineno, node.module, alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "lcsas":
                    yield node.lineno, alias.name, None


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (
        name.startswith("__") and name.endswith("__")
    )


def _sources() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 50, f"found only {len(files)} modules under {SRC}"
    return files


def test_no_private_name_imported_across_packages() -> None:
    offenders: list[str] = []
    for path in _sources():
        here = _package(_module_name(path))
        for line, module, name in _imports(path):
            if _package(module) == here:
                continue
            private_module = any(
                _is_private(p) for p in module.split(".")[1:]
            )
            if private_module or (name is not None and _is_private(name)):
                shown = f"{module}.{name}" if name else module
                offenders.append(f"{path.relative_to(SRC)}:{line}: {shown}")
    assert not offenders, (
        "Private names imported from another package (give them a public "
        "name in their home module):\n  " + "\n  ".join(offenders)
    )


def test_private_import_check_catches_an_offender(tmp_path: Path) -> None:
    """The scanner itself sees a nested cross-package private import."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def f():\n    from lcsas.iso.xorriso import _hidden\n"
    )
    found = [(m, n) for _, m, n in _imports(probe)]
    assert found == [("lcsas.iso.xorriso", "_hidden")]
    assert _is_private("_hidden") and not _is_private("__version__")


def test_only_restore_leaves_are_imported_from_lower_layers() -> None:
    offenders: list[str] = []
    for path in _sources():
        module = _module_name(path)
        here = _package(module)
        if here in _FRONT_ENDS or here == "restore":
            continue
        for line, target, _name in _imports(path):
            if _package(target) == "restore" and target not in _RESTORE_PUBLIC:
                offenders.append(f"{path.relative_to(SRC)}:{line}: {target}")
    assert not offenders, (
        "Only restore.restic_fallback and restore.standalone_builder may be "
        "imported outside restore/, cli/ and meta/:\n  "
        + "\n  ".join(offenders)
    )


def test_restore_leaves_import_no_other_lcsas_code() -> None:
    offenders: list[str] = []
    for leaf, allowed in _RESTORE_LEAVES.items():
        path = SRC / (leaf.split(".", 1)[1].replace(".", "/") + ".py")
        assert path.is_file(), path
        for line, target, _name in _imports(path):
            if target not in allowed:
                offenders.append(f"{leaf}:{line}: {target}")
    assert not offenders, (
        "A restore leaf imports other LCSAS code, so the burn/staging code "
        "that imports it now depends on more of restore/ (and the tier-3 "
        "standalone restorer may break):\n  " + "\n  ".join(offenders)
    )


def test_binpack_does_not_import_iso() -> None:
    for path in sorted((SRC / "binpack").rglob("*.py")):
        for line, target, _name in _imports(path):
            assert _package(target) != "iso", (
                f"{path.relative_to(SRC)}:{line} imports {target}"
            )
