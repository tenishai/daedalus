"""Where the agent's bundle comes from at start (one runtime, bundles as artifacts).

With BUNDLE_ARTIFACT set (``<registry>/<path>/<agent>-bundle:<tag>`` or ``@sha256:…``), the
bundle is pulled from that OCI artifact into BUNDLE_DIR (default /techne/bundle, on the
agent's own volume so a registry that cannot be reached at a later restart still leaves
the last pulled bundle to run from). Without it, the image's own /bundle (argv[1]) is used,
as before. Either way the directory to run from is printed on stdout, and that is the only
thing the entrypoint reads.

The artifact is what the room publishes: one layer, a gzip tarball of the bundle directory
(media type application/vnd.tenish.agent-bundle.tar+gzip). The registry is asked over plain
HTTP with a Google access token when one is there (google-auth, the metadata server), so the
agent's own service account, the identity that already pulls its image, is the one that
pulls its bundle; without google-auth the pull is anonymous.

usage: bundle_pull.py <fallback-dir>
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

ARTIFACT_ENV = "BUNDLE_ARTIFACT"
DIR_ENV = "BUNDLE_DIR"
DEFAULT_DIR = "/techne/bundle"
LAYER_MEDIA_TYPE = "application/vnd.tenish.agent-bundle.tar+gzip"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DIGEST_FILE = ".bundle-digest"
TIMEOUT_S = 60.0


def parse_reference(reference: str) -> tuple[str, str, str]:
    """``host/path/name:tag`` or ``host/path/name@sha256:…`` -> (host, repository, reference)."""
    reference = reference.strip()
    host, _, rest = reference.partition("/")
    if not host or not rest:
        raise ValueError(f"not a registry reference: {reference!r}")
    if "@" in rest:
        repo, _, ref = rest.partition("@")
    elif ":" in rest.rsplit("/", 1)[-1]:
        repo, _, ref = rest.rpartition(":")
    else:
        repo, ref = rest, "latest"
    return host, repo, ref


def access_token() -> str | None:
    """A Google access token from the environment the pod runs in, or None."""
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError:
        return None
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token or None
    except Exception as exc:  # noqa: BLE001 -- anonymous is the fallback, and the registry says no
        print(
            f"bundle: no access token ({type(exc).__name__}); pulling anonymously", file=sys.stderr
        )
        return None


def _get(
    url: str, headers: dict[str, str], opener=urllib.request.urlopen
) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers)
    with opener(req, timeout=TIMEOUT_S) as resp:
        return resp.read(), {k.lower(): v for k, v in resp.headers.items()}


def pick_layer(manifest: dict) -> dict:
    """The bundle tarball layer: the one with our media type, else the single layer."""
    layers = [layer for layer in manifest.get("layers", []) if isinstance(layer, dict)]
    ours = [layer for layer in layers if layer.get("mediaType") == LAYER_MEDIA_TYPE]
    if ours:
        return ours[0]
    if len(layers) == 1:
        return layers[0]
    raise ValueError(f"the artifact has {len(layers)} layers and none is a bundle tarball")


def extract(tarball: bytes, dest: Path) -> None:
    """Unpack into a fresh directory beside `dest`, then swap it in, so a half-written
    bundle is never what runs. Paths that would escape `dest` are refused."""
    fresh = dest.with_name(dest.name + ".next")
    if fresh.exists():
        _rmtree(fresh)
    fresh.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (fresh / member.name).resolve()
            if (
                not str(target).startswith(str(fresh.resolve()) + os.sep)
                and target != fresh.resolve()
            ):
                raise ValueError(f"refusing a tarball entry outside the bundle: {member.name!r}")
        tar.extractall(fresh, filter="data")
    old = dest.with_name(dest.name + ".old")
    if old.exists():
        _rmtree(old)
    if dest.exists():
        dest.rename(old)
    fresh.rename(dest)
    if old.exists():
        _rmtree(old)


def _rmtree(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
    path.rmdir()


def pull(reference: str, dest: Path, *, token: str | None, opener=urllib.request.urlopen) -> str:
    """Pull the artifact into `dest`; returns the manifest digest. Raises on any refusal."""
    host, repo, ref = parse_reference(reference)
    headers = {"Accept": MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body, resp_headers = _get(f"https://{host}/v2/{repo}/manifests/{ref}", headers, opener)
    manifest = json.loads(body)
    digest = resp_headers.get("docker-content-digest", "")
    layer = pick_layer(manifest)
    blob, _ = _get(f"https://{host}/v2/{repo}/blobs/{layer['digest']}", headers, opener)
    extract(blob, dest)
    (dest / DIGEST_FILE).write_text(digest + "\n")
    return digest


def main(argv: list[str]) -> int:
    fallback = argv[1] if len(argv) > 1 else "/bundle"
    reference = os.environ.get(ARTIFACT_ENV, "").strip()
    if not reference:
        print(fallback)
        return 0
    dest = Path(os.environ.get(DIR_ENV) or DEFAULT_DIR)
    try:
        digest = pull(reference, dest, token=access_token())
        print(
            f"bundle: pulled {reference} ({digest or 'no digest header'}) into {dest}",
            file=sys.stderr,
        )
    except (
        urllib.error.URLError,
        ValueError,
        OSError,
        json.JSONDecodeError,
        tarfile.TarError,
    ) as exc:
        if (dest / "agent.yaml").exists():
            print(
                f"bundle: could not pull {reference} ({exc}); "
                f"running the last pulled bundle in {dest}",
                file=sys.stderr,
            )
        else:
            print(
                f"bundle: could not pull {reference} ({exc}) and nothing was pulled before",
                file=sys.stderr,
            )
            return 1
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
