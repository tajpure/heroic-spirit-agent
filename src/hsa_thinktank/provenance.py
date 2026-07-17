"""Safe, deterministic normalisation for model-declared claim provenance."""

from __future__ import annotations

import copy
import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_HTTP_URL = re.compile(r"(?i)^https?://")
_NON_PUBLIC_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost", ".home")
_PROVENANCE_FIELDS = (
    "principle_ids",
    "evidence_ids",
    "memory_ids",
    "tool_artifact_ids",
    "source_urls",
)


class ProvenanceNormalizationError(ValueError):
    """A public citation looked unsafe or malformed.

    The message deliberately never includes the rejected value because runtime
    errors are persisted in the privileged audit bundle.
    """


@dataclass(frozen=True)
class ProvenanceNormalizationResult:
    payload: dict[str, Any]
    normalizations: tuple[dict[str, str], ...]


def normalize_public_source_url(value: Any) -> str:
    """Return a canonical public HTTP(S) URL or raise a redacted error."""

    if not isinstance(value, str):
        value = str(value)
    candidate = value.strip()
    if not candidate or any(character.isspace() or ord(character) < 32 for character in candidate):
        raise ProvenanceNormalizationError("public citation URL is malformed")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        raise ProvenanceNormalizationError("public citation URL is malformed") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ProvenanceNormalizationError("public citation URL must use HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ProvenanceNormalizationError("public citation URL cannot contain credentials")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise ProvenanceNormalizationError("public citation URL host is malformed") from None
    if not host or host == "localhost" or host.endswith(_NON_PUBLIC_HOST_SUFFIXES):
        raise ProvenanceNormalizationError("public citation URL host is not public")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ProvenanceNormalizationError("public citation URL host is not public") from None
        rendered_host = host
    else:
        if not address.is_global:
            raise ProvenanceNormalizationError("public citation URL host is not public")
        rendered_host = f"[{host}]" if address.version == 6 else host

    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        rendered_host = f"{rendered_host}:{port}"
    path = parsed.path or "/"
    normalized = urlunsplit((scheme, rendered_host, path, "", ""))
    if len(normalized) > 2048:
        raise ProvenanceNormalizationError("public citation URL is too long")
    return normalized


def normalize_provenance_payload(payload: dict[str, Any]) -> ProvenanceNormalizationResult:
    """Normalise only claim-shaped dictionaries in a copied response payload."""

    copied = copy.deepcopy(payload)
    applied: list[dict[str, str]] = []
    _visit(copied, applied=applied)
    return ProvenanceNormalizationResult(payload=copied, normalizations=tuple(applied))


def _visit(value: Any, *, applied: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        if "claim" in value and any(
            field in value for field in ("basis", "tool_artifact_ids", "source_urls")
        ):
            _normalize_claim(value, applied=applied)
        for nested in list(value.values()):
            _visit(nested, applied=applied)
    elif isinstance(value, list):
        for nested in value:
            _visit(nested, applied=applied)


def _normalize_claim(
    claim: dict[str, Any],
    *,
    applied: list[dict[str, str]],
) -> None:
    source_values = claim.get("source_urls", [])
    if not isinstance(source_values, list):
        return
    normalized_sources: list[str] = []
    for value in source_values:
        normalized = normalize_public_source_url(value)
        if normalized not in normalized_sources:
            normalized_sources.append(normalized)
        if not isinstance(value, str) or normalized != value:
            applied.append(_normalization("source_url_canonicalized"))

    artifact_values = claim.get("tool_artifact_ids", [])
    if isinstance(artifact_values, list):
        opaque_artifacts: list[Any] = []
        for value in artifact_values:
            if isinstance(value, str) and _HTTP_URL.match(value.strip()):
                normalized = normalize_public_source_url(value)
                if normalized not in normalized_sources:
                    normalized_sources.append(normalized)
                applied.append(_normalization("url_artifact_moved_to_source"))
            else:
                opaque_artifacts.append(value)
        claim["tool_artifact_ids"] = opaque_artifacts

    if normalized_sources or "source_urls" in claim:
        claim["source_urls"] = normalized_sources
    if claim.get("basis") == "grounded" and _has_no_provenance(claim):
        claim["basis"] = "inferred"
        applied.append({"code": "grounded_without_provenance_downgraded"})


def _has_no_provenance(claim: dict[str, Any]) -> bool:
    for field in _PROVENANCE_FIELDS:
        if field not in claim:
            continue
        value = claim[field]
        if not isinstance(value, list) or value:
            return False
    return True


def _normalization(code: str) -> dict[str, str]:
    return {"code": code}


__all__ = [
    "ProvenanceNormalizationError",
    "ProvenanceNormalizationResult",
    "normalize_provenance_payload",
    "normalize_public_source_url",
]
