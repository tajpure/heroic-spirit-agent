## ADDED Requirements

### Requirement: Distinguish public citations from verified runtime artifacts
The system SHALL represent public HTTP(S) citations separately from opaque tool artifact IDs.
A public citation SHALL be syntax-validated, restricted to a public network host, and canonicalised
without userinfo, query, or fragment data, but SHALL NOT be presented as runtime-verified. An
opaque tool artifact ID SHALL be accepted only when the current runtime response contains a tool
artifact with the same ID.

#### Scenario: Public webpage citation
- **WHEN** an HSA returns an HTTP(S) URL as support for a claim
- **THEN** the system records it as a public citation and does not require a matching opaque tool artifact

#### Scenario: Fabricated opaque artifact
- **WHEN** an HSA references a non-URL tool artifact ID absent from the current runtime response
- **THEN** the system rejects that contribution as referencing unavailable runtime evidence

### Requirement: Recover safe provenance-shape mistakes deterministically
The system SHALL normalise known, semantics-preserving provenance mistakes before model
validation. It SHALL move HTTP(S) values out of `tool_artifact_ids` into public citations and SHALL
downgrade a `grounded` claim with no provenance to `inferred`. It SHALL record each normalisation in
the private audit trace using only a stable operation code and SHALL NOT rewrite the claim text or
persist the source value or model-provided JSON path in that audit metadata.

#### Scenario: URL placed in artifact IDs
- **WHEN** a claim places a valid public URL in `tool_artifact_ids`
- **THEN** the contribution is validated with that URL recorded as a public citation

#### Scenario: Public URL contains query credentials
- **WHEN** an otherwise public citation includes query or fragment data
- **THEN** the stored citation omits that data and the normalisation audit contains no original value

#### Scenario: Grounded claim lacks all references
- **WHEN** a claim is marked `grounded` and contains no principle, evidence, memory, public citation, or runtime artifact reference
- **THEN** the system validates it as `inferred` and records the downgrade without changing its text

### Requirement: Carry sanitised tool artifacts across the Hermes bridge
For every Hermes-native tool completion observed by the bridge, the system SHALL create a stable
artifact metadata record and return it in `RawAgentResponse.tool_artifacts`. The record SHALL
contain an ID, tool name, result size, and result hash, and SHALL NOT contain the raw result body,
credentials, or tool arguments.

#### Scenario: Hermes tool completes
- **WHEN** the bridge observes a completed Hermes tool call
- **THEN** the parent runtime receives matching sanitised metadata in both the tool event and final tool artifact collection

#### Scenario: Older bridge omits artifacts
- **WHEN** a bridge response contains no tool artifact collection
- **THEN** the runtime treats it as an empty collection without failing the invocation

### Requirement: Use one evidence contract in every evidence-bearing phase
Option generation and all ballot, memo, critique, rebuttal, and executive phases SHALL use the same
claim provenance semantics. Option generation MAY omit claims when it only describes candidate
actions, but any explicit supporting claim SHALL use the shared evidence contract.

#### Scenario: Option generation includes supporting facts
- **WHEN** a chair returns supporting claims with generated options
- **THEN** those claims receive the same normalisation and reference validation as later member claims
