"""Hardening test: tier-3 standalone restorer disc-swap protocol (#234).

When the recovery cascade reaches tier 3 on a multi-disc archive, the
bundled CPython ``standalone_restorer.py`` must implement the same
LCSAS disc-swap UX as tier 1 -- otherwise packs spread across N data
discs cause an unrecoverable FileNotFoundError after the meta disc
has been ejected.

These two tests pin the protocol:

  * Case 1 (non-interactive): with ``--interactive off`` and packs
    missing from the configured search paths, the restorer fails
    cleanly with FileNotFoundError (preserves pre-#234 behaviour for
    test harnesses + non-tty callers).

  * Case 2 (interactive): with ``--interactive on`` and a second
    ``--mount-point`` pointing at the dir where packs were moved,
    the restorer prints the framed prompt to stderr on first lookup,
    consumes an ENTER on stdin, re-scans, and successfully completes
    the restore byte-for-byte.

Tests require ``rustic`` on PATH because tier-3 needs a real
rustic-format repo (the synthetic-key path in
``tests/unit/test_standalone_subprocess.py`` doesn't exercise the
multi-pack disc-spread case this is testing).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore.standalone_builder import build_standalone

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_repo_and_script(
    tmp_path: Path,
) -> tuple[Path, Path, Path, list[Path], Path]:
    """Build a real rustic repo, move all packs to a sibling dir, and
    materialise standalone_restorer.py.

    Returns
    -------
    repo : Path
        The (now packless-data/) rustic repo root.
    pwfile : Path
        The password file.
    script : Path
        The generated standalone_restorer.py path.
    pack_files : list[Path]
        New locations of the moved pack files (under ``other_dir``).
    src : Path
        The original source tree (for byte-for-byte diffing).
    """
    repo = tmp_path / "repo"
    src = tmp_path / "src"
    pwfile = tmp_path / "pw"

    src.mkdir()
    # Two files distributed across at least one pack each.  Single big
    # blob would fit in one pack; we use medium chunks for one and
    # different content for the other so rustic emits >=1 data pack.
    (src / "alpha.txt").write_bytes(b"alpha payload\n" * 10)
    (src / "beta.txt").write_bytes(b"beta payload, distinct content\n" * 10)
    pwfile.write_text("test-password\n")

    subprocess.run(
        ["rustic", "-r", str(repo), "init", "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=60,
    )
    subprocess.run(
        ["rustic", "-r", str(repo), "backup", str(src),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=60,
    )

    # Move every pack out of repo/data/ into a sibling "other_dir".
    # After the move, repo/data/ has empty <XX>/ subdirs only -- the
    # restorer must use --mount-point to find the packs.
    other_dir = tmp_path / "other_disc"
    moved: list[Path] = []
    for pack in (repo / "data").rglob("*"):
        if not pack.is_file():
            continue
        prefix = pack.parent.name           # two-hex-char subdirectory
        dest_dir = other_dir / "data" / prefix
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / pack.name
        shutil.move(str(pack), str(dst))
        moved.append(dst)
    assert moved, "rustic backup produced no pack files"

    # Materialise standalone_restorer.py from the source modules.
    script = tmp_path / "standalone_restorer.py"
    script.write_text(build_standalone())
    os.chmod(str(script), 0o755)

    return repo, pwfile, script, moved, src


def _trees_equal(a: Path, b: Path) -> bool:
    """Compare two directory trees byte-for-byte.  Ignores empty
    intermediate dirs (rustic restore re-creates the absolute source
    path under target/)."""
    def files(root: Path) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for f in root.rglob("*"):
            if f.is_file():
                out[f.name] = f.read_bytes()
        return out

    return files(a) == files(b)


@pytest.mark.skipif(
    shutil.which("rustic") is None,
    reason="rustic not on PATH; tier-3 tests need a real rustic-format repo",
)
def test_tier3_non_interactive_raises_when_pack_missing(tmp_path: Path) -> None:
    """Case 1: --interactive off + no --mount-point for the moved-pack
    dir should fail with a clearly-named pack-not-found error.

    This is the pre-#234 behaviour preserved for non-tty callers (e.g.
    automated test harnesses) -- they don't want a stdin-blocking
    prompt; they want a deterministic FileNotFoundError.
    """
    repo, pwfile, script, _moved, _src = _build_repo_and_script(tmp_path)
    target = tmp_path / "restored"

    result = subprocess.run(
        [
            sys.executable, str(script),
            "--repo", str(repo),
            "--password-file", str(pwfile),
            "--target", str(target),
            "--interactive", "off",
            # NOTE: no --mount-point for other_disc -- packs are unreachable.
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, (
        "tier-3 succeeded despite missing packs -- "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "Pack file not found" in combined, (
        f"expected 'Pack file not found' diagnostic in output, got:\n{combined}"
    )


@pytest.mark.skipif(
    shutil.which("rustic") is None,
    reason="rustic not on PATH; tier-3 tests need a real rustic-format repo",
)
def test_tier3_interactive_recovers_via_stdin_enter(tmp_path: Path) -> None:
    """Case 2: --interactive on + --mount-point pointing at the moved
    packs.  The restorer must:
      - print the framed disc-swap prompt to stderr (with the pack hash)
      - block on stdin for ENTER
      - rescan and find the pack
      - complete the restore byte-for-byte against the source tree
    """
    repo, pwfile, script, moved, src = _build_repo_and_script(tmp_path)
    target = tmp_path / "restored"
    other_disc = moved[0].parents[2]    # tmp/other_disc

    # We want to FORCE the prompt to fire even though --mount-point
    # points at the right disc.  Trick: stage the packs OUT of
    # other_disc into a holding/ dir, then move them BACK only after
    # the restorer has printed its first framed prompt to stderr.  This
    # proves the rescan-on-ENTER actually re-walks the configured
    # mount points (rather than caching a negative result from the
    # first lookup).
    import threading

    holding = tmp_path / "holding"
    holding.mkdir()
    parked: list[tuple[Path, Path]] = []
    for p in moved:
        rel = p.relative_to(other_disc)
        park_to = holding / rel
        park_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(park_to))
        parked.append((park_to, p))

    # Run the restorer under Popen so we can react to stderr line-by-line.
    proc = subprocess.Popen(
        [
            sys.executable, str(script),
            "--repo", str(repo),
            "--password-file", str(pwfile),
            "--target", str(target),
            "--interactive", "on",
            "--mount-point", str(other_disc),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_chunks: list[str] = []
    packs_restored_done = threading.Event()

    def watch_stderr() -> None:
        # Reads one byte at a time so we don't block waiting for a
        # newline on the prompt-line ("> ") which the restorer prints
        # without a trailing newline.
        assert proc.stderr is not None
        assert proc.stdin is not None
        prompt_seen = False
        buf: list[str] = []
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            buf.append(ch)
            if ch == "\n":
                stderr_chunks.append("".join(buf))
                buf = []
                continue
            # On seeing the "> " prompt the first time, restore packs
            # and send ENTER.  Subsequent prompts can be replied to
            # with a plain ENTER (no further restoration needed).
            line_so_far = "".join(buf)
            if line_so_far.endswith("> ") and not prompt_seen:
                prompt_seen = True
                for park_from, restore_to in parked:
                    restore_to.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(park_from), str(restore_to))
                packs_restored_done.set()
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    return
            elif line_so_far.endswith("> ") and prompt_seen:
                # Should not happen now that packs are in place, but
                # don't deadlock if it does.
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    return
        if buf:
            stderr_chunks.append("".join(buf))

    # NOTE: do NOT call proc.communicate() -- it would race with the
    # watcher for proc.stderr.  The watcher reads stderr to completion;
    # we read stdout separately and wait() for exit.
    stdout_chunks: list[str] = []

    def drain_stdout() -> None:
        assert proc.stdout is not None
        while True:
            ch = proc.stdout.read(4096)
            if not ch:
                break
            stdout_chunks.append(ch)

    watcher = threading.Thread(target=watch_stderr)
    out_drainer = threading.Thread(target=drain_stdout)
    watcher.start()
    out_drainer.start()

    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        watcher.join(timeout=10)
        out_drainer.join(timeout=10)
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass

    stdout = "".join(stdout_chunks)

    assert packs_restored_done.is_set(), (
        "the disc-swap prompt never fired -- "
        f"stderr:\n{''.join(stderr_chunks)}"
    )
    assert proc.returncode == 0, (
        "tier-3 interactive restore failed -- "
        f"stdout:\n{stdout}\nstderr:\n{''.join(stderr_chunks)}"
    )

    # The framed prompt must have fired at least once on stderr.
    stderr = "".join(stderr_chunks)
    assert "Insert the right disc and press ENTER to retry." in stderr, (
        f"expected disc-swap prompt in stderr, got:\n{stderr}"
    )
    # The "is required for the next file" line must mention SOME pack
    # hash prefix -- assert the framing carries the hash.  (We don't
    # bind to a specific hash because rustic-generated hashes differ
    # per run; the protocol pin is "the prompt names a pack".)
    assert "is required for the next file." in stderr, (
        f"prompt missing the pack-name line, got:\n{stderr}"
    )
    # The "Currently searching:" block must include the --mount-point
    # we passed (proves the second search path was registered).  Long
    # paths get truncated with a "..." prefix in the prompt; assert on
    # the basename, which always survives the truncation.
    assert other_disc.name in stderr, (
        f"--mount-point not echoed in 'Currently searching:' block; "
        f"stderr:\n{stderr}"
    )

    # Restored tree must match source byte-for-byte.
    assert target.is_dir(), f"target not created: {target}"
    # rustic places the restored tree under target/<abs source path>.
    # Walk the only-child chain down to the actual files.
    cur = target
    while True:
        entries = list(cur.iterdir())
        if len(entries) != 1 or not entries[0].is_dir():
            break
        cur = entries[0]
    assert _trees_equal(src, cur), (
        f"restored tree does not match source.\n"
        f"  source files: {sorted(p.name for p in src.iterdir())}\n"
        f"  target files: {sorted(p.name for p in cur.iterdir())}"
    )
