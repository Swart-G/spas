"""Run safety regressions and check the real Executor port in this checkout."""

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("MYPYPATH", None)
    for pattern in ("test_safety_checker.py", "test_agent_permissions.py", "test_executor*.py"):
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"],
            cwd=root,
            env=env,
            check=True,
        )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--follow-imports=silent",
            "--explicit-package-bases",
            "executer/safety_checker/checker.py",
            "executer/safety_checker/permissions.py",
            "shared/contracts/safety.py",
            "tests/typecheck_executor_port.py",
        ],
        cwd=root,
        env=env,
        check=True,
    )
    print("Safety regressions and Executor port typing passed.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
