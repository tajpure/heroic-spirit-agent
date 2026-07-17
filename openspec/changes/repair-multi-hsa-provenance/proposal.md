## Why

Real Hermes meetings currently accept the chair's option-generation message but reject every
formal contribution when the model either cites a public URL as a tool artifact or marks a claim
as grounded without an opaque provenance ID. The failure is systematic: the prompt does not
explain the cross-field rule and the Hermes bridge never returns the `tool_artifacts` collection
that the orchestrator requires.

## What Changes

- Introduce an explicit, machine-valid public-citation field separate from runtime-verified tool
  artifact IDs.
- Deterministically normalise common safe output mistakes before validation: public HTTP(S) URLs
  are treated as citations, while unsupported `grounded` claims are downgraded to `inferred`
  instead of discarding the member's entire contribution.
- Carry completed Hermes tool metadata through the bridge into `RawAgentResponse.tool_artifacts`
  without persisting tool result bodies or credentials.
- Apply the same evidence semantics to option generation so the chair cannot bypass the contract.
- Preserve strict rejection for fabricated principle, evidence, memory, and opaque artifact IDs.
- Keep internal validation details out of the TUI while ensuring every usable member contribution,
  disagreement, and final decision state remains visible.
- Add regression coverage based on the two observed real-run failure shapes and bridge-level tool
  events.

## Capabilities

### New Capabilities

- `hsa-evidence-contract`: Defines public citations, runtime-verified tool artifacts, safe
  normalisation, and evidence requirements shared by every meeting phase.
- `meeting-contribution-reliability`: Defines how selected HSA contributions survive recoverable
  formatting mistakes, how unrecoverable failures affect quorum, and what the TUI presents.

### Modified Capabilities

None; this repository did not previously contain checked-in OpenSpec capabilities.

## Impact

The change affects the Pydantic response models, prompt compiler, Hermes NDJSON bridge, runtime
accumulator, orchestration validation, aggregation/report bindings, TUI event projection, and their
unit/integration tests. The bridge protocol remains backward compatible: new artifact fields are
optional for older bridge responses, and no raw tool result body is added to persisted run data.
