"""The blind harness hides leftover directories by ACL, not chown/chmod.

``setup.py`` hides leftover directories on the host (``LEAK_DIRS``) from
the agent user so they cannot leak the illusion.  It used to
``chown root:root`` and ``chmod 0700`` them, and ``teardown.sh`` put back
mode 755 but never the owner.  ``/scratch/cargo-target`` belongs to another
project on the same host: every one of its builds failed with "Permission
denied" while a blind variant ran, and the directory stayed root-owned
afterwards (2026-10-31).

Now setup adds one ACL entry, ``u:lcsas-blind:---``, and teardown removes
it before ``userdel``, so owner and mode are never touched.  These tests
check that in the source (static) and run the exact setfacl step on a
scratch directory to show it keeps owner and mode.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tests" / "e2e" / "cdemu_blind_restore"
SETUP_PY = HARNESS / "setup.py"
TEARDOWN_SH = HARNESS / "teardown.sh"


def _setup_tree() -> ast.Module:
    return ast.parse(SETUP_PY.read_text(encoding="utf-8"))


def _leak_dirs() -> list[str]:
    """setup.py's LEAK_DIRS, as path strings."""
    for node in _setup_tree().body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "LEAK_DIRS"
                        for t in node.targets)):
            return [
                call.args[0].value
                for call in ast.walk(node.value)
                if isinstance(call, ast.Call) and call.args
                and isinstance(call.args[0], ast.Constant)
            ]
    raise AssertionError("setup.py lost its LEAK_DIRS constant")


def _leak_loop() -> ast.For:
    """The ``for leak in LEAK_DIRS`` loop in setup.py."""
    for node in ast.walk(_setup_tree()):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
                and node.iter.id == "LEAK_DIRS"):
            return node
    raise AssertionError("setup.py no longer loops over LEAK_DIRS")


def _calls(node: ast.AST) -> list[str]:
    """Dotted names of every call inside ``node`` (``os.chown``, ...)."""
    names = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            names.append(ast.unparse(call.func))
    return names


def test_leak_dirs_include_the_shared_cargo_target() -> None:
    assert "/scratch/cargo-target" in _leak_dirs()


def test_setup_never_chowns_or_chmods_a_leak_dir() -> None:
    loop = _leak_loop()
    calls = _calls(loop)
    assert not {"os.chown", "os.chmod", "shutil.chown"} & set(calls), calls
    assert "subprocess.run" in calls
    source = ast.unparse(loop)
    assert "setfacl" in source and "u:{AGENT_USER}:---" in source, source
    # And no other chown/chmod anywhere in setup.py names a leak dir.
    text = SETUP_PY.read_text(encoding="utf-8")
    for leak in _leak_dirs():
        for m in re.finditer(r"os\.ch(?:own|mod)\(([^)]*)\)", text):
            assert leak not in m.group(1), m.group(0)


def test_teardown_drops_the_acl_before_userdel_and_leaves_mode_alone() -> None:
    text = TEARDOWN_SH.read_text(encoding="utf-8")
    m = re.search(r"^for d in (.*); do\n.*setfacl -x u:lcsas-blind",
                  text, re.MULTILINE)
    assert m, "teardown.sh no longer removes the agent's ACL entry"
    assert sorted(m.group(1).split()) == sorted(_leak_dirs()), (
        "teardown.sh's setfacl -x list must match setup.py's LEAK_DIRS"
    )
    assert m.start() < text.index("userdel"), (
        "the ACL entry must be removed while the user name still resolves"
    )
    for loop in re.finditer(r"^for d in (.*); do\n(.*)\n", text, re.MULTILINE):
        if "chmod" in loop.group(2) or "chown" in loop.group(2):
            for leak in _leak_dirs():
                assert leak not in loop.group(1).split(), loop.group(0)


@pytest.mark.skipif(shutil.which("setfacl") is None,
                    reason="setfacl(1) not installed")
def test_the_acl_step_keeps_owner_and_mode(tmp_path: Path) -> None:
    """Run setup's setfacl step on a scratch dir (user ``nobody``)."""
    leak = tmp_path / "cargo-target"
    leak.mkdir()
    leak.chmod(0o755)
    before = os.stat(leak)
    add = subprocess.run(["setfacl", "-m", "u:nobody:---", str(leak)],
                         capture_output=True, text=True)
    if add.returncode != 0:
        pytest.skip(f"no ACL support here: {add.stderr.strip()}")
    after = os.stat(leak)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o7777 == 0o755
    acl = subprocess.run(["getfacl", "-p", str(leak)],
                         capture_output=True, text=True, check=True).stdout
    assert "user:nobody:---" in acl
    subprocess.run(["setfacl", "-x", "u:nobody", str(leak)], check=True)
    acl = subprocess.run(["getfacl", "-p", str(leak)],
                         capture_output=True, text=True, check=True).stdout
    assert "user:nobody" not in acl
    assert os.stat(leak).st_mode & 0o7777 == 0o755
