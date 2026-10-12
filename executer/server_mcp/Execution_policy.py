"""Фиксированные права запуска. CWD сам по себе не является изоляцией."""

import os
from pathlib import Path
import stat


COMMAND_UID = 10001
COMMAND_GID = 10001
WORKSPACE = Path("/workspace")
IMAGE_MARKER = Path("/etc/spas-executor")


def subprocess_options() -> dict:
    """Отказать вне защищённого контейнера; параметры не принимаются от Worker."""
    if not IMAGE_MARKER.is_file() or os.geteuid() != 0:
        raise RuntimeError("Use Run_docker.sh: execution outside the protected container is disabled")

    status = dict(
        line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    # Контроллеру нужны только KILL, SETGID и SETUID. Дочерний процесс
    # теряет capabilities при переходе с root на непривилегированный UID.
    if status.get("NoNewPrivs", "").strip() != "1" or int(status["CapEff"], 16) != 0xE0:
        raise RuntimeError("Protected capability/no-new-privileges settings are missing")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("Container root filesystem must be read-only")
    # Контроллер должен войти в cwd до смены UID дочернего процесса.
    # Root владеет каталогом, группа executor получает rwx, остальные — ничего.
    info = WORKSPACE.lstat()
    required_flags = os.ST_NOEXEC | os.ST_NOSUID | os.ST_NODEV
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != COMMAND_GID
        or stat.S_IMODE(info.st_mode) != 0o770
        or os.statvfs(WORKSPACE).f_flag & required_flags != required_flags
    ):
        raise RuntimeError("Protected workspace permissions or mount flags are missing")
    return {
        "cwd": str(WORKSPACE),
        "user": COMMAND_UID,
        "group": COMMAND_GID,
        "extra_groups": (),
        "umask": 0o077,
        "close_fds": True,
        # Не наследуем ключи, BASH_ENV, PYTHONPATH, LD_PRELOAD и другие
        # переменные MCP-контроллера. Все значения заданы оператором в коде.
        "env": {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(WORKSPACE),
            "TMPDIR": str(WORKSPACE),
            "USER": "executor",
            "LOGNAME": "executor",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    }


def validate_read_paths(argv: tuple[str, ...]) -> None:
    """В checked-режиме читаем только явные пути без симлинков и спецфайлов.

    Входной mount управляется доверенным оператором: его нельзя менять во время
    запроса. Проверка lstat не защищает от гонки с администратором хоста.
    """
    if argv[0] not in {"ls", "cat"}:
        return
    paths = [arg for arg in argv[1:] if not arg.startswith("-")]
    for name in paths:
        path = Path(name)
        if str(path) != name or ".." in path.parts:
            raise ValueError("Non-canonical read path")
        if path != WORKSPACE and not path.is_relative_to("/inputs"):
            raise ValueError("Read outside input scope")
        for part in [*reversed(path.parents), path]:
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Symlink reads are forbidden")
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or (argv[0] == "ls" and stat.S_ISDIR(info.st_mode))):
            raise ValueError("Unsupported read target")
