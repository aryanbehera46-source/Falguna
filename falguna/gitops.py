import subprocess
from pathlib import Path
from typing import List


class GitWorktreeManager:
    def __init__(self, source_repo: Path, root: Path):
        self.source_repo = Path(source_repo).resolve()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, run_id: str, base_ref: str = "HEAD") -> Path:
        target = self.root / run_id
        branch = f"falguna/run-{run_id}"
        subprocess.run(["git", "-C", str(self.source_repo), "worktree", "add", "-b", branch, str(target), base_ref], check=True, capture_output=True, text=True)
        return target

    def create_confidential(self, run_id: str, base_ref: str, hidden_relative_paths: List[str], stash_root: Path) -> Path:
        """Phase 3 audit (Requirement 1) fix for a real, demonstrated gap: a
        plain `create()` worktree shares source_repo's object database, so
        physically removing a hidden file from the WORKING TREE (the
        pre-existing stash/restore mechanism) does not stop a git-capable
        worker from retrieving it with `git show <ref>:<path>`, `git log -p`,
        or `git cat-file --batch-all-objects` -- verified live during this
        audit (git worktree add off a repo that already had the file
        committed leaves it fully recoverable via git plumbing regardless of
        the working-tree copy). The only defensible fix is that the worker's
        worktree never share an object store that ever contained the hidden
        blob at all.

        This builds a fully independent clone (--no-hardlinks: never share
        object files with source_repo, even via inode aliasing), removes the
        hidden paths, squashes history to a single orphan commit so no other
        commit/branch/tag in this clone's history can reference the old
        blob, deletes every other ref (local branches, remote-tracking
        refs, tags) so nothing keeps the pre-scrub commit reachable, expires
        the reflog, and runs `git gc --prune=now` to physically remove the
        now-unreachable objects from disk. Only then is the worker's actual
        worktree created, off this scrubbed clone -- never off source_repo.
        source_repo itself is never touched (a fresh clone, never a
        worktree of it), so this cannot corrupt or affect the real
        repository. Called only when RunPolicy.hidden_verification_files is
        set; every other mission continues to use plain create()."""
        scrub_root = self.root / f"{run_id}-scrub"

        def _git(*args, cwd=scrub_root):
            return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)

        subprocess.run(["git", "clone", "--no-hardlinks", "-q", str(self.source_repo), str(scrub_root)], check=True, capture_output=True, text=True)
        _git("checkout", "-q", base_ref)
        for relative in hidden_relative_paths:
            # Seed the stash directly from source_repo's own committed blob at
            # base_ref (not from this scrub clone, and not from a worktree --
            # this repo is never given a worktree with the file present, so
            # there is nothing else to move out of it). This is what
            # ControlPlane._restore_hidden_files (falguna/orchestrator.py)
            # later moves back into the worker's worktree, once independent
            # verification is the only thing that still needs it.
            blob = subprocess.run(
                ["git", "-C", str(self.source_repo), "show", f"{base_ref}:{relative}"],
                capture_output=True, check=False,
            )
            if blob.returncode == 0:
                stash_target = stash_root / relative
                stash_target.parent.mkdir(parents=True, exist_ok=True)
                stash_target.write_bytes(blob.stdout)
            if (scrub_root / relative).is_file():
                _git("rm", "-q", "-f", "--", relative)
        scrubbed_branch = "_falguna_scrubbed_base"
        _git("checkout", "-q", "--orphan", scrubbed_branch)
        _git("-c", "user.email=falguna-scrub@local", "-c", "user.name=falguna-scrub", "commit", "-q", "-m", "Falguna confidentiality scrub (hidden verification files removed)")
        refs = _git("for-each-ref", "--format=%(refname)").stdout.splitlines()
        for ref in refs:
            ref = ref.strip()
            if ref and ref != f"refs/heads/{scrubbed_branch}":
                subprocess.run(["git", "-C", str(scrub_root), "update-ref", "-d", ref], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(scrub_root), "remote", "remove", "origin"], capture_output=True, text=True)  # best-effort, refs already deleted above
        _git("reflog", "expire", "--expire=now", "--all")
        _git("gc", "--prune=now", "--aggressive", "-q")

        target = self.root / run_id
        branch = f"falguna/run-{run_id}"
        subprocess.run(["git", "-C", str(scrub_root), "worktree", "add", "-q", "-b", branch, str(target), scrubbed_branch], check=True, capture_output=True, text=True)
        return target

    def head(self, worktree: Path) -> str:
        return subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()

    def changed_files(self, worktree: Path) -> List[str]:
        # Enumerate untracked files individually. Confidential worktrees restore
        # hidden verification files only after the worker exits, and those files
        # are intentionally absent from the scrubbed Git history. Git's default
        # porcelain output collapses such files to an untracked directory (for
        # example ``tests/``), which cannot be matched against the exact hidden
        # path and is then correctly rejected by the write allowlist. Reporting
        # every file keeps changed-file accounting exact without widening policy.
        output = subprocess.check_output(
            ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
            text=True,
        )
        return [line[3:] for line in output.splitlines() if line]

    def diff(self, worktree: Path) -> str:
        return subprocess.check_output(["git", "-C", str(worktree), "diff", "--no-ext-diff", "--binary", "HEAD"], text=True)
