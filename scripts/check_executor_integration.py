"""Verify both components in one temporary checkout, never modify either source."""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TEST_FILES = (
    "test_safety_checker.py",
    "test_executor_integration.py",
    "typecheck_executor_port.py",
)


def assemble(checker: Path, executor: Path, destination: Path) -> None:
    """Keep Executor's package initializer and overlay only checker components."""
    for name in ("Models.py", "Safety_contract.py", "Server.py", "Runner.py"):
        if not (executor / "executer" / "server_mcp" / name).is_file():
            raise ValueError(f"Executor checkout is missing server_mcp/{name}")
    destination.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(executor / "executer", destination / "executer", ignore=ignore)
    if (executor / "shared").is_dir():
        shutil.copytree(executor / "shared", destination / "shared", ignore=ignore)
    shutil.copytree(
        checker / "executer" / "safety_checker",
        destination / "executer" / "safety_checker",
        dirs_exist_ok=True,
        ignore=ignore,
    )
    shutil.copytree(
        checker / "shared", destination / "shared", dirs_exist_ok=True, ignore=ignore,
    )
    (destination / "tests").mkdir()
    for name in _TEST_FILES:
        shutil.copy2(checker / "tests" / name, destination / "tests" / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executor_checkout", type=Path)
    args = parser.parse_args()
    checker = Path(__file__).resolve().parents[1]
    executor = args.executor_checkout.resolve()
    missing = [name for name in ("mcp", "mcp_types", "pydantic", "mypy")
               if importlib.util.find_spec(name) is None]
    if missing:
        parser.error("Install Executor dependencies and mypy; missing: " + ", ".join(missing))
    with tempfile.TemporaryDirectory(prefix="spas-executor-check-") as temporary:
        destination = Path(temporary) / "checkout"
        try:
            assemble(checker, executor, destination)
        except ValueError as error:
            parser.error(str(error))
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("MYPYPATH", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # The child starts from this single assembled root. No second checkout
        # is added to PYTHONPATH; Executor's regular package remains intact.
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=destination, env=env, check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "mypy", "--strict", "--follow-imports=silent",
             "--explicit-package-bases", "executer/safety_checker/checker.py",
             "shared/contracts/safety.py", "tests/typecheck_executor_port.py"],
            cwd=destination, env=env, check=True,
        )
    print("Executor integration and port typing checks passed; source checkouts unchanged.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
