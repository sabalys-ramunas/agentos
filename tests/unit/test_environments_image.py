"""The docker provider's image from a plain install: the prebuilt one for this version downloads and
is named as vms.toml's docker_image; when it cannot, the image is built here from the Dockerfile the
package carries (openajar.environments.cli._get_image)."""

import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from openajar import __version__
from openajar.environments import cli as envcli
from openajar.vms.docker import IMAGE_VERSION
from openajar.vms.models import VmSettings

ROOT = Path(__file__).resolve().parents[2]
PULL = VmSettings().docker_pull


class Docker:
    def __init__(self, *, pull_ok=True, label=str(IMAGE_VERSION)):
        self.pull_ok, self.label, self.calls = pull_ok, label, []

    def run(self, args, **_kw):
        self.calls.append(args[1:3])
        if args[1] == "pull":
            return SimpleNamespace(returncode=0 if self.pull_ok else 1, stdout="",
                                   stderr="" if self.pull_ok else "Error response from daemon: manifest unknown")
        if args[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=self.label + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def built(monkeypatch):
    calls = []
    monkeypatch.setattr(envcli, "_build_image", lambda image, docker: calls.append(image))
    return calls


def test_the_prebuilt_image_is_this_versions_and_takes_the_local_name(monkeypatch, built, capsys):
    docker = Docker()
    monkeypatch.setattr(subprocess, "run", docker.run)
    envcli._get_image("agentos-env:latest", "docker", PULL)
    source = f"ghcr.io/sabalys-ramunas/agentos-env:{__version__}"
    assert docker.calls == [["pull", "-q"], ["image", "inspect"], ["tag", source]]
    assert built == [] and "Downloaded agentos-env:latest." in capsys.readouterr().out


@pytest.mark.parametrize("docker", [Docker(pull_ok=False), Docker(label="1"), Docker(label="")],
                         ids=["no image to pull", "an older image", "not an openajar image"])
def test_without_a_good_download_it_builds_here(monkeypatch, built, capsys, docker):
    monkeypatch.setattr(subprocess, "run", docker.run)
    envcli._get_image("agentos-env:latest", "docker", PULL)
    assert built == ["agentos-env:latest"] and ["tag"] not in [c[:1] for c in docker.calls]
    out = capsys.readouterr().out
    assert "Building it here instead." in out and "—" not in out


def test_an_empty_docker_pull_never_downloads(monkeypatch, built):
    docker = Docker()
    monkeypatch.setattr(subprocess, "run", docker.run)
    envcli._get_image("agentos-env:latest", "docker", "")
    assert docker.calls == [] and built == ["agentos-env:latest"]


def test_the_wheel_carries_the_dockerfile_so_a_plain_install_can_build():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included["deploy/environments"] == "openajar/environments/image"
    assert (ROOT / "deploy/environments/Dockerfile").exists()
    assert envcli._image_source() is not None


def test_the_workflow_publishes_where_docker_pull_looks():
    workflow = (ROOT / ".github/workflows/env-image.yml").read_text()
    assert "ghcr.io/${{ github.repository_owner }}/agentos-env" in workflow
    assert PULL == "ghcr.io/sabalys-ramunas/agentos-env:{version}"
