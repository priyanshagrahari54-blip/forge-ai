import subprocess


class GitTool:
    def __init__(self, repo: str = ".") -> None:
        self.repo = repo

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )

    def status(self) -> str:
        result = self.run("status", "--short")
        return result.stdout.strip()
