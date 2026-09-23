"""Image pinning.

Scored trials must never resolve a floating tag. Registry images are pinned by
repository digest (``repo@sha256:...``). The locally built application image has
no registry digest, so it is pinned by its immutable content-addressed image ID
(``sha256:...``), which Docker accepts as an image reference and reports back
verbatim on every container it creates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .docker_cli import DockerError, docker

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class PinnedImage:
    """An image reference that cannot drift between runs."""

    service: str
    reference: str
    image_id: str
    kind: str
    source: str
    repo_digest: str | None = field(default=None)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "reference": self.reference,
            "imageId": self.image_id,
            "repoDigest": self.repo_digest,
            "kind": self.kind,
            "source": self.source,
        }


def is_digest(value: str) -> bool:
    return bool(_DIGEST_RE.match(value or ""))


def _image_id(reference: str) -> str:
    result = docker("image", "inspect", "--format", "{{.Id}}", reference, timeout=60)
    image_id = result.stdout.strip()
    if not is_digest(image_id):
        raise DockerError(f"unexpected image id for {reference}: {image_id!r}")
    return image_id


def _repo_digest(reference: str) -> str:
    result = docker(
        "image", "inspect", "--format", "{{range .RepoDigests}}{{.}}\n{{end}}", reference, timeout=60
    )
    digests = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not digests:
        raise DockerError(
            f"{reference} has no repository digest; it must be pulled from a registry before pinning"
        )
    repository = reference.split(":", 1)[0]
    for digest in digests:
        if digest.split("@", 1)[0] == repository:
            return digest
    return digests[0]


def resolve_registry_image(service: str, tag: str, *, pull: bool = True) -> PinnedImage:
    """Pull a floating tag once and convert it into a digest pin."""
    if "@" in tag:
        reference = tag
        if pull:
            docker("pull", "--quiet", reference, timeout=900)
        return PinnedImage(
            service=service,
            reference=reference,
            image_id=_image_id(reference),
            kind="registry",
            source=tag,
            repo_digest=reference,
        )

    if pull:
        docker("pull", "--quiet", tag, timeout=900)
    repo_digest = _repo_digest(tag)
    return PinnedImage(
        service=service,
        reference=repo_digest,
        image_id=_image_id(repo_digest),
        kind="registry",
        source=tag,
        repo_digest=repo_digest,
    )


def build_local_image(service: str, context_dir: str, tag: str) -> PinnedImage:
    """Build the application image once and pin it by content-addressed ID."""
    docker(
        "build",
        "--quiet",
        "--tag",
        tag,
        context_dir,
        timeout=1800,
    )
    image_id = _image_id(tag)
    return PinnedImage(
        service=service,
        reference=image_id,
        image_id=image_id,
        kind="local-build",
        source=tag,
        repo_digest=None,
    )


def verify_container_image(container: str, expected: PinnedImage) -> tuple[bool, str]:
    """Confirm from the daemon that a container runs the exact pinned image."""
    result = docker("inspect", "--format", "{{.Image}}", container, timeout=60)
    actual = result.stdout.strip()
    return actual == expected.image_id, actual
