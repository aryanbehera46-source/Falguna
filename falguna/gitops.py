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

    def head(self, worktree: Path) -> str:
        return subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()

    def changed_files(self, worktree: Path) -> List[str]:
        output = subprocess.check_output(["git", "-C", str(worktree), "status", "--porcelain"], text=True)
        return [line[3:] for line in output.splitlines() if line]

    def diff(self, worktree: Path) -> str:
        return subprocess.check_output(["git", "-C", str(worktree), "diff", "--no-ext-diff", "--binary", "HEAD"], text=True)

