"""Build the check image only on a local Docker engine."""

import json
import os
import subprocess
import sys


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def local_engine():
    endpoint = os.environ.get("DOCKER_HOST")
    selected = os.environ.get("DOCKER_CONTEXT")
    if selected or not endpoint:
        context = docker("context", "inspect", *([selected] if selected else []), capture_output=True)
        endpoint = json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise ValueError("Use a local Docker engine (unix/npipe); remote Docker is refused")
    docker("info", stdout=subprocess.DEVNULL)


def main():
    local_engine()
    docker("build", "-t", "invoice-bot:check", ".")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("Docker image check failed:", error, file=sys.stderr)
        sys.exit(1)
