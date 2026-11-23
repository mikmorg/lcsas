"""#954: the manual steps of RESTORE_INSTRUCTIONS.txt restore the files.

Every data disc carries RESTORE_INSTRUCTIONS.txt, and the data disc's
START_HERE.txt sends the heir there when the META disc is missing.  Its
step 3 copied packs with a loop over ``data/*``; packs are one level
deeper (``data/<2 hex>/<sha256>``), so the loop copied nothing, used a
bash-only substitution, and the ``rustic restore`` of step 5 crashed.

This test reads the steps out of the file a real stage wrote, runs them
as written under POSIX ``sh`` (only ``/tmp/`` is moved into the test's
folder and the placeholders are filled in), against disc trees extracted
from the real ISOs with xorriso (read-only files, as on a disc), with a
stock rustic, and compares the restore with the backed-up files.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _manual_steps(text: str) -> dict[int, list[str]]:
    """The command lines of each numbered step of "HOW TO RESTORE
    (manual)": the lines indented by 7 spaces under the step."""
    section = text.split("HOW TO RESTORE (manual)", 1)[1]
    section = section.split("\nDISC CONTENTS", 1)[0]
    steps: dict[int, list[str]] = {}
    current = 0
    for line in section.splitlines():
        m = re.match(r"^(\d+)\. ", line)
        if m:
            current = int(m.group(1))
            steps[current] = []
            continue
        if current and line.startswith("       ") and line.strip():
            steps[current].append(line[7:])
    return steps


def _run_sh(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", "-ec", script], capture_output=True,
                          text=True, env=env, timeout=600)


def test_manual_steps_restore_a_two_disc_set(tmp_path: Path) -> None:
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    for i in range(3):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.backup("alpha", "small.txt", size=3000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    isos = sorted(inst.staging.rglob("*.iso"))
    assert len(isos) >= 2, "the set must span more than one disc"

    work = tmp_path / "heir"
    tmp = work / "tmp"          # stands in for /tmp
    tmp.mkdir(parents=True)
    text = (inst.disc_trees()[inst.labels()[0]]
            / "RESTORE_INSTRUCTIONS.txt").read_text()
    steps = _manual_steps(text)

    def fill(cmd: str) -> str:
        return (cmd.replace("/tmp/", f"{tmp}/")
                .replace("REPO_ID", inst.repo_id("alpha"))
                .replace("~/password.txt", str(inst.key("alpha")))
                .replace(" /output ", f" {work / 'output'} "))

    env = {"PATH": os.environ["PATH"], "HOME": str(work),
           "TMPDIR": str(tmp)}
    # Step 1 once per disc (vol1, vol2, ...): option C (xorriso), and
    # option B (7z) for the second disc when 7z is there.
    (xorriso_line,) = [c for c in steps[1] if c.startswith("xorriso")]
    (sevenz_line,) = [c for c in steps[1] if c.startswith("7z ")]
    for n, iso in enumerate(isos, 1):
        line = sevenz_line if n == 2 and shutil.which("7z") else xorriso_line
        cmd = (fill(line.replace("/vol1/", f"/vol{n}/"))
               .replace("VOLUME.iso", str(iso)))
        run = _run_sh(cmd, env)
        assert run.returncode == 0, run.stderr
    # Steps 3 to 5 as written, in one POSIX shell.
    script = "\n".join(fill(c) for s in (3, 4, 5) for c in steps[s]
                       if not c.startswith("grep "))
    run = _run_sh(script, env)
    assert run.returncode == 0, (script, run.stdout, run.stderr)

    expected = tree_digest(inst.data("alpha"))
    restored = tree_digest(work / "output")
    got = {k.rsplit("/", 1)[-1]: v for k, v in restored.items()}
    assert got == expected, (sorted(restored), sorted(expected))

    # "HOW TO RESTORE (pure Python)": the disc's python_reader.py on the
    # same cache, its command as written.
    py_section = text.split("HOW TO RESTORE (pure Python", 1)[1]
    py_section = py_section.split("HOW TO RESTORE (manual)", 1)[0]
    cmd = " ".join(line.strip().rstrip("\\").strip()
                   for line in py_section.splitlines()
                   if line.strip().startswith(("python3 ", "--password-file")))
    reader = inst.disc_trees()[inst.labels()[0]] / "python_reader.py"
    cmd = (cmd.replace("/tmp/", f"{tmp}/")
           .replace("python_reader.py", str(reader))
           .replace("/path/to/password.txt", str(inst.key("alpha")))
           .replace("/path/to/output", str(work / "output-py")))
    run = _run_sh(cmd, env)
    assert run.returncode == 0, (cmd, run.stderr[-2000:])
    restored = tree_digest(work / "output-py")
    got = {k.rsplit("/", 1)[-1]: v for k, v in restored.items()}
    assert got == expected, sorted(restored)


def test_standard_tools_runbook_restores_a_two_disc_set(tmp_path: Path) -> None:
    """The META disc's RESTORE_STANDARD_TOOLS.txt (zero LCSAS code), steps
    2 and 4 as written, with stock restic, on discs extracted read-only."""
    if not shutil.which("restic"):
        pytest.skip("stock restic not on PATH")
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    for i in range(3):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    isos = sorted(inst.staging.rglob("*.iso"))
    assert len(isos) >= 2
    work = tmp_path / "heir"
    discs = []
    for n, iso in enumerate(reversed(isos), 1):     # disc1 = the newest
        disc = work / f"disc{n}"
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(iso),
                        "-extract", "/", str(disc)],
                       capture_output=True, check=True)
        discs.append(disc)

    runbook = (Path(__file__).resolve().parents[2] / "recovery" / "docs"
               / "RESTORE_STANDARD_TOOLS.txt").read_text()

    def block(start: str, end: str) -> list[str]:
        part = runbook.split(start, 1)[1].split(end, 1)[0]
        return [ln[4:] for ln in part.splitlines()
                if ln.startswith("    ") and ln.strip()
                and not ln.strip().startswith("#")]

    tmp = work / "tmp"
    tmp.mkdir()
    step2 = block("reassemble one restic repository from the discs",
                  "You now have")
    step4 = block("restore with stock restic", "Your files are now")
    script = []
    for line in step2 + step4:
        line = line.split("  #", 1)[0].rstrip()
        if line.startswith("export RESTIC_PASSWORD="):
            script.append(f"export RESTIC_PASSWORD_FILE={inst.key('alpha')}")
            continue
        line = (line.replace("/tmp/", f"{tmp}/")
                .replace("for d in /mnt/disc1 /mnt/disc2 /mnt/disc3",
                         "for d in " + " ".join(map(str, discs)))
                .replace("/mnt/disc1", str(discs[0]))
                .replace("<repository-id>", inst.repo_id("alpha")))
        script.append(line)
    env = {"PATH": os.environ["PATH"], "HOME": str(work),
           "TMPDIR": str(tmp), "RESTIC_CACHE_DIR": str(work / "cache")}
    run = _run_sh("\n".join(script), env)
    assert run.returncode == 0, ("\n".join(script), run.stderr[-3000:])
    restored = tree_digest(tmp / "restored")
    got = {k.rsplit("/", 1)[-1]: v for k, v in restored.items()}
    assert got == tree_digest(inst.data("alpha")), sorted(restored)
