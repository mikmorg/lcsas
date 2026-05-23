"""Helpers for the tier-1 ↔ tier-2 differential oracle (Phase 14).

Three pure functions:

  build_rustic_repo(src, repo, pwfile) -> snapshot_id
      Runs `rustic init` + `rustic backup`.

  restore_with_tier1(repo, target, pwfile, restore_bin)
  restore_with_tier2(repo, target, pwfile)
      Run the named binary's restore against repo, write into target.

  diff_trees(a, b, ignore=()) -> list[str]
      Walk both directory trees; return a list of human-readable
      difference strings.  Empty list = identical.

Used by tests/recovery_hardening/test_tier1_vs_tier2_differential.py.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_BIN_CANDIDATES = (
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64-linux-musl" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
)


def find_restore_bin() -> Path | None:
    """Return the first runnable lcsas-restore on the conventional paths,
    or None if nothing has been built yet."""
    for p in RESTORE_BIN_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def find_restored_root(target: Path) -> Path:
    """Both tier-1 and tier-2 may place the restored tree under
    `<target>/<abs_src_path>/`.  Walk down until we hit a directory
    with more than one entry OR a non-directory entry; that's the
    effective root.  Both restorers follow the same convention so
    callers can compare like-for-like."""
    cur = target
    while True:
        try:
            entries = list(cur.iterdir())
        except FileNotFoundError:
            return target
        if len(entries) != 1:
            return cur
        only = entries[0]
        if not only.is_dir() or only.is_symlink():
            return cur
        cur = only


def build_rustic_repo(src: Path, repo: Path, pwfile: Path) -> str:
    """Run `rustic init` then `rustic backup <src>`.  Returns the
    snapshot id reported by the backup command (or the empty string
    if rustic didn't print one)."""
    subprocess.run(
        ["rustic", "-r", str(repo), "init",
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=60,
    )
    backup_res = subprocess.run(
        ["rustic", "-r", str(repo), "backup", str(src),
         "--password-file", str(pwfile)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    # Best-effort: pull "snapshot <id>" out of stdout/stderr.
    for line in (backup_res.stdout + backup_res.stderr).splitlines():
        line = line.strip()
        if line.startswith("snapshot ") and len(line) >= 16:
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return ""


def restore_with_tier1(repo: Path, target: Path, pwfile: Path,
                       restore_bin: Path) -> None:
    """Invoke `lcsas-restore --repo <repo> --target <target>
    --password-file <pw>`.  Raises CalledProcessError on non-zero
    exit."""
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(restore_bin),
         "--repo", str(repo),
         "--target", str(target),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=300,
    )


def restore_with_tier2(repo: Path, target: Path, pwfile: Path) -> None:
    """Invoke `rustic -r <repo> restore latest <target>
    --password-file <pw>`.  Raises CalledProcessError on non-zero
    exit."""
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rustic", "-r", str(repo), "restore", "latest", str(target),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=300,
    )


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_relative(root: Path) -> dict[str, Path]:
    """Map every entry under `root` (excluding root itself) to its
    absolute path, keyed by POSIX-style relative path."""
    out: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            out[rel] = full
        # On modern Python os.walk returns symlinks-to-dirs as
        # dirnames; followlinks=False keeps them as nodes but doesn't
        # descend.  Capture them above.
    return out


_MTIME_TOLERANCE_SEC = 2.0


def diff_trees(a: Path, b: Path,
               ignore: tuple[str, ...] = (),
               compare_mtime: bool = False) -> list[str]:
    """Compare two directory trees for tier-1↔tier-2 parity.

    Compares: presence in both, regular-file content (sha256), file
    mode (st_mode & 0o7777), symlink target.  When ``compare_mtime``
    is set, also compares mtime with a ±2 s tolerance window (issue
    #188).  The default is False so that callers added before tier-1
    learned to preserve mtime keep their pre-existing semantics.
    Returns a list of human-readable difference strings; empty list
    means identical."""
    ignore_set = set(ignore)
    side_a = {k: v for k, v in _walk_relative(a).items() if k not in ignore_set}
    side_b = {k: v for k, v in _walk_relative(b).items() if k not in ignore_set}

    out: list[str] = []

    only_a = sorted(set(side_a) - set(side_b))
    only_b = sorted(set(side_b) - set(side_a))
    for rel in only_a:
        out.append(f"only in tier1: {rel}")
    for rel in only_b:
        out.append(f"only in tier2: {rel}")

    for rel in sorted(set(side_a) & set(side_b)):
        pa, pb = side_a[rel], side_b[rel]
        sa, sb = pa.lstat(), pb.lstat()
        ma, mb = stat.S_IFMT(sa.st_mode), stat.S_IFMT(sb.st_mode)
        if ma != mb:
            out.append(f"type mismatch: {rel} (tier1=0o{ma:o} tier2=0o{mb:o})")
            continue

        perm_a = sa.st_mode & 0o7777
        perm_b = sb.st_mode & 0o7777
        if perm_a != perm_b:
            out.append(
                f"mode mismatch: {rel} (tier1=0o{perm_a:o} tier2=0o{perm_b:o})"
            )

        if stat.S_ISLNK(sa.st_mode):
            ta = os.readlink(pa)
            tb = os.readlink(pb)
            if ta != tb:
                out.append(
                    f"symlink target mismatch: {rel} (tier1={ta!r} tier2={tb!r})"
                )
        elif stat.S_ISREG(sa.st_mode):
            if sa.st_size != sb.st_size:
                out.append(
                    f"size mismatch: {rel} (tier1={sa.st_size} tier2={sb.st_size})"
                )
                continue
            ha = _sha256_file(pa)
            hb = _sha256_file(pb)
            if ha != hb:
                out.append(
                    f"content mismatch: {rel} (tier1 sha256={ha[:12]}… "
                    f"tier2 sha256={hb[:12]}…)"
                )
        # Directories: nothing more to compare beyond mode + presence.

        # mtime parity is opt-in.  Symlinks are exempt because not
        # every filesystem honours AT_SYMLINK_NOFOLLOW timestamps and
        # the divergence isn't load-bearing for the caller use cases.
        if compare_mtime and not stat.S_ISLNK(sa.st_mode):
            dt = abs(sa.st_mtime - sb.st_mtime)
            if dt > _MTIME_TOLERANCE_SEC:
                out.append(
                    f"mtime mismatch: {rel} (tier1={sa.st_mtime:.3f} "
                    f"tier2={sb.st_mtime:.3f} diff={dt:.3f}s)"
                )

    return out
