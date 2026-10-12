#!/usr/bin/env python3
"""Build in the cloud sandbox using its proxy DNS and verified CA bundle."""

import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    environment = os.environ.copy()
    environment.setdefault("DOCKER_CONFIG", "/workspace/.cache/docker")
    command = ["docker", "build", "--network=host", "-t", "safety-analyzer:dev"]
    hosts = set()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if name in environment:
            # Pass names, not proxy values, as arguments; Docker reads them from the environment.
            command.extend(["--build-arg", name])
            if name.lower() != "no_proxy":
                hostname = urlsplit(environment[name]).hostname
                if hostname:
                    hosts.add(hostname)
    for host in sorted(hosts):
        address = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        command.extend(["--add-host", f"{host}:{address}"])
    if ca_file := environment.get("SSL_CERT_FILE"):
        command.extend(["--secret", f"id=build_ca,src={ca_file}"])
    command.append(".")
    completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
