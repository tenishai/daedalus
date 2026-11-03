"""The runtime pulls its bundle from a registry artifact at start (docker/bundle_pull.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "bundle_pull.py"


@pytest.fixture
def pull_module():
    spec = importlib.util.spec_from_file_location("bundle_pull", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body, self.headers = body, headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _registry(files: dict[str, str], *, media_type: str, digest: str = "sha256:abc"):
    """A fake opener serving one manifest and one blob; records the requests."""
    blob = _tarball(files)
    manifest = {
        "schemaVersion": 2,
        "layers": [{"mediaType": media_type, "digest": "sha256:layer", "size": len(blob)}],
    }
    seen: list[tuple[str, dict]] = []

    def opener(req, timeout=None):
        seen.append((req.full_url, dict(req.headers)))
        if "/manifests/" in req.full_url:
            return _Response(json.dumps(manifest).encode(), {"Docker-Content-Digest": digest})
        if "/blobs/sha256:layer" in req.full_url:
            return _Response(blob, {})
        raise AssertionError(req.full_url)

    return opener, seen


def test_a_reference_names_host_repository_and_tag_or_digest(pull_module):
    assert pull_module.parse_reference("us-east4-docker.pkg.dev/p/r/ledger-bundle:current") == (
        "us-east4-docker.pkg.dev",
        "p/r/ledger-bundle",
        "current",
    )
    assert pull_module.parse_reference("reg.example/r/x-bundle@sha256:deadbeef") == (
        "reg.example",
        "r/x-bundle",
        "sha256:deadbeef",
    )
    assert pull_module.parse_reference("reg.example:5000/r/x-bundle:v1")[0] == "reg.example:5000"
    with pytest.raises(ValueError):
        pull_module.parse_reference("nonsense")


def test_the_bundle_is_pulled_into_place_with_the_token_and_swapped_in_whole(pull_module, tmp_path):
    opener, seen = _registry(
        {"agent.yaml": "name: x\n", "persona.md": "hi"}, media_type=pull_module.LAYER_MEDIA_TYPE
    )
    dest = tmp_path / "bundle"
    dest.mkdir()
    (dest / "stale.md").write_text("from an older pull")
    digest = pull_module.pull("reg.example/r/x-bundle:current", dest, token="tok", opener=opener)
    assert digest == "sha256:abc"
    assert (dest / "agent.yaml").read_text() == "name: x\n" and (
        dest / "persona.md"
    ).read_text() == "hi"
    assert not (dest / "stale.md").exists()  # the old directory is replaced, not merged
    assert (dest / pull_module.DIGEST_FILE).read_text().strip() == "sha256:abc"
    assert all(h.get("Authorization") == "Bearer tok" for _, h in seen) and len(seen) == 2
    assert "application/vnd.oci.image.manifest.v1+json" in seen[0][1]["Accept"]


def test_a_single_layer_of_any_type_is_taken_and_an_escaping_entry_is_refused(
    pull_module, tmp_path
):
    opener, _ = _registry(
        {"agent.yaml": "name: y\n"}, media_type="application/vnd.oci.image.layer.v1.tar+gzip"
    )
    pull_module.pull("reg.example/r/y-bundle:current", tmp_path / "b", token=None, opener=opener)
    assert (tmp_path / "b" / "agent.yaml").exists()
    opener, _ = _registry({"../escape": "no"}, media_type=pull_module.LAYER_MEDIA_TYPE)
    with pytest.raises(ValueError, match="outside the bundle"):
        pull_module.pull(
            "reg.example/r/z-bundle:current", tmp_path / "c", token=None, opener=opener
        )


def test_main_prints_the_fallback_without_an_artifact_and_the_last_pull_when_the_registry_is_away(
    pull_module, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv(pull_module.ARTIFACT_ENV, raising=False)
    assert pull_module.main(["bundle_pull.py", "/bundle"]) == 0
    assert capsys.readouterr().out.strip() == "/bundle"

    dest = tmp_path / "pulled"
    monkeypatch.setenv(pull_module.ARTIFACT_ENV, "reg.example/r/x-bundle:current")
    monkeypatch.setenv(pull_module.DIR_ENV, str(dest))
    monkeypatch.setattr(pull_module, "access_token", lambda: None)

    def away(*a, **k):
        raise pull_module.urllib.error.URLError("no route")

    monkeypatch.setattr(pull_module.urllib.request, "urlopen", away)
    assert pull_module.main(["bundle_pull.py", "/bundle"]) == 1  # nothing pulled before
    dest.mkdir()
    (dest / "agent.yaml").write_text("name: x\n")
    assert pull_module.main(["bundle_pull.py", "/bundle"]) == 0
    assert capsys.readouterr().out.strip().endswith("pulled")
