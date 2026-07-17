from __future__ import annotations

import pytest

from hsa_thinktank.provenance import (
    ProvenanceNormalizationError,
    normalize_provenance_payload,
    normalize_public_source_url,
)


def test_url_artifact_moves_to_public_source_without_creating_runtime_evidence() -> None:
    raw = {
        "claims": [
            {
                "claim": "Public filing supports the estimate",
                "basis": "grounded",
                "tool_artifact_ids": ["HTTPS://Example.COM:443/filing#section"],
            }
        ]
    }

    result = normalize_provenance_payload(raw)

    assert raw["claims"][0]["tool_artifact_ids"] == ["HTTPS://Example.COM:443/filing#section"]
    assert result.payload["claims"][0] == {
        "claim": "Public filing supports the estimate",
        "basis": "grounded",
        "tool_artifact_ids": [],
        "source_urls": ["https://example.com/filing"],
    }
    assert result.normalizations[0]["code"] == "url_artifact_moved_to_source"
    assert "HTTPS://Example.COM" not in str(result.normalizations)
    assert result.normalizations[0] == {"code": "url_artifact_moved_to_source"}


def test_grounded_claim_without_any_reference_is_downgraded_without_text_rewrite() -> None:
    raw = {
        "ballot": {
            "claims": [
                {
                    "claim": "Diversification reduces concentration risk",
                    "basis": "grounded",
                    "principle_ids": [],
                    "evidence_ids": [],
                    "memory_ids": [],
                    "tool_artifact_ids": [],
                }
            ]
        }
    }

    result = normalize_provenance_payload(raw)

    claim = result.payload["ballot"]["claims"][0]
    assert claim["claim"] == raw["ballot"]["claims"][0]["claim"]
    assert claim["basis"] == "inferred"
    assert result.normalizations == ({"code": "grounded_without_provenance_downgraded"},)


def test_unknown_opaque_artifact_is_not_removed_or_downgraded() -> None:
    raw = {
        "claims": [
            {
                "claim": "Unsupported",
                "basis": "grounded",
                "tool_artifact_ids": ["made-up-artifact"],
            }
        ]
    }

    result = normalize_provenance_payload(raw)

    assert result.payload == raw
    assert result.normalizations == ()


def test_attack_url_artifact_moves_to_source_without_inventing_a_basis() -> None:
    raw = {
        "attacks": [
            {
                "claim": "The alternative has a documented failure mode",
                "tool_artifact_ids": ["https://example.com/failure-mode"],
            }
        ]
    }

    result = normalize_provenance_payload(raw)

    assert result.payload["attacks"][0] == {
        "claim": "The alternative has a documented failure mode",
        "tool_artifact_ids": [],
        "source_urls": ["https://example.com/failure-mode"],
    }
    assert "basis" not in result.payload["attacks"][0]
    assert result.normalizations[0]["code"] == "url_artifact_moved_to_source"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/private",
        "http://localhost/source",
        "http://127.0.0.1/source",
        "https://user:password@example.com/source",
    ],
)
def test_public_source_url_rejects_non_public_or_sensitive_values(url: str) -> None:
    with pytest.raises(ProvenanceNormalizationError) as caught:
        normalize_public_source_url(url)

    assert url not in str(caught.value)


def test_public_source_url_canonicalization_is_deterministic() -> None:
    assert (
        normalize_public_source_url(
            " HTTPS://Example.COM:443/research?access_token=private#details "
        )
        == "https://example.com/research"
    )
