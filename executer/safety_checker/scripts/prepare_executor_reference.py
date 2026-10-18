"""Export pinned real Rule-Based Checker/Executor sources for offline contract tests."""

import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

REVISION = "00b54e3ebfb768ebfdf0ee69304879b3cb1c7c7b"
COMPONENT = Path(__file__).resolve().parents[1]
REPOSITORY = COMPONENT.parents[1]
DESTINATION = COMPONENT / "reports" / "reference" / REVISION


def main() -> None:
    if (DESTINATION / ".complete").is_file():
        print("Pinned Executor reference already prepared")
        return
    present = subprocess.run(
        ["git", "cat-file", "-e", REVISION + "^{commit}"],
        cwd=REPOSITORY,
        capture_output=True,
        check=False,
    )
    if present.returncode:
        subprocess.run(["git", "fetch", "origin", REVISION], cwd=REPOSITORY, check=True)
    archive = subprocess.check_output(
        [
            "git",
            "archive",
            REVISION,
            "executer/__init__.py",
            "executer/safety_checker/checker.py",
            "executer/safety_checker/__init__.py",
            "executer/server_mcp",
            "shared",
        ],
        cwd=REPOSITORY,
    )
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DESTINATION.parent) as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(temporary, filter="data")
        Path(temporary, ".complete").write_text(REVISION, encoding="utf-8")
        Path(temporary).rename(DESTINATION)
    print("Prepared pinned real Executor/Rule-Based Checker reference")


if __name__ == "__main__":
    main()
