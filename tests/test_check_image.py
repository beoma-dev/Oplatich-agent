"""Prevent accidental builds on a workstation's production Docker context."""

import json
import subprocess
from unittest.mock import patch

import pytest

from tools import check_image


def test_remote_host_refused_before_any_docker_command(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    for endpoint in ("ssh://production", "tcp://production:2375"):
        monkeypatch.setenv("DOCKER_HOST", endpoint)
        with patch.object(check_image, "docker") as docker:
            with pytest.raises(ValueError, match="remote Docker"):
                check_image.main()
            docker.assert_not_called()


def test_remote_context_overrides_local_host_and_never_builds(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///local.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "production")
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps([
        {"Endpoints": {"docker": {"Host": "ssh://production"}}}
    ]))
    with patch.object(check_image, "docker", return_value=result) as docker:
        with pytest.raises(ValueError, match="remote Docker"):
            check_image.main()
        assert [c.args for c in docker.call_args_list] == [("context", "inspect", "production")]


def test_local_context_overrides_inherited_remote_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://unused-production")
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps([
        {"Endpoints": {"docker": {"Host": "unix:///local.sock"}}}
    ]))
    with patch.object(check_image, "docker", return_value=result) as docker:
        check_image.main()
        assert docker.call_args_list[-1].args == ("build", "-t", "invoice-bot:check", ".")


def test_missing_docker_engine_does_not_build(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///missing.sock")
    with patch.object(check_image, "docker", side_effect=OSError("no engine")) as docker:
        with pytest.raises(OSError):
            check_image.main()
        assert [c.args for c in docker.call_args_list] == [("info",)]


def test_build_error_is_not_swallowed(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///local.sock")
    with patch.object(check_image, "docker", side_effect=[None, subprocess.CalledProcessError(4, "docker build")]):
        with pytest.raises(subprocess.CalledProcessError):
            check_image.main()
