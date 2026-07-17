## Context

The orchestrator intentionally validates model output after every Hermes invocation. In the current
implementation, `RationaleClaim` has a Pydantic-only cross-field rule that is absent from the JSON
Schema sent to the model. At the same time, the Hermes event bridge emits tool progress but discards
the completed tool metadata before constructing `RawAgentResponse`. Real Codex-backed runs therefore
produce valid deliberation content that is rejected after the paid invocation has completed.

The user-facing requirement is to see what each HSA said, what they disputed, and the resulting
decision. Internal evidence controls must remain fail closed for frozen and opaque references, but
implementation-specific provenance formatting must not erase otherwise usable contributions.

## Goals / Non-Goals

**Goals:**

- Preserve auditability while accepting public citations and safely downgrading unsupported claims.
- Transport sanitised Hermes-native tool artifact metadata end to end.
- Make the observed two- and three-member meetings count every recoverable contribution.
- Keep validation and confidence mechanics out of the normal TUI conversation.
- Avoid additional paid calls for deterministic repairs.

**Non-Goals:**

- Treating a model-declared URL as proof that a page was fetched.
- Persisting raw tool output or credentials in the run bundle.
- Retrying a dispatched model call or changing provider concurrency in this change.
- Repairing unrelated red-team quorum semantics.

## Decisions

### Public citations are a separate provenance type

`RationaleClaim` gains `source_urls`, containing validated public HTTP(S) URLs. Canonicalisation
removes userinfo, query, and fragment data so a citation cannot persist a bearer token or another
secret embedded in the URL. These citations count as declared provenance for `grounded` claims but
remain visibly distinct in persisted JSON from runtime-verified `tool_artifact_ids`. This is safer
than blessing a URL as an opaque artifact and more useful than forcing every current-events claim
to become `inferred`.

### Normalisation is deterministic and audit-only

Before Pydantic validation, a recursive normaliser processes only claim-shaped dictionaries. It
moves URL-shaped artifact IDs into `source_urls` and changes `basis` from `grounded` to `inferred`
only when every provenance list is empty. It never changes claim text, scores, preferences,
constraints, or unknown opaque IDs. Only stable operation codes are included in the private runtime
audit record—never the source value, its hash, or a model-provided JSON path. The TUI receives only
the resulting accepted contribution.

An additional model repair call was rejected because it adds cost, latency, and duplicate-call
risk. Silently disabling reference validation was rejected because it would accept fabricated
frozen IDs.

### Bridge artifacts contain metadata, not results

The bridge derives a stable artifact ID from the tool call identity, tool name, and result hash. It
adds that ID to the `tool.completed` frame. The parent validates each optional record, accumulates a
deduplicated collection for `RawAgentResponse.tool_artifacts`, and stores only ID, name, size, and
SHA-256. Missing fields from an older bridge mean an empty artifact list, preserving compatibility.

Codex app-server's internal tools do not currently invoke Hermes' native tool callbacks. Their
public citations therefore use `source_urls`; they are not mislabeled as verified tool artifacts.
The bridge-derived `hta_*` ID is currently visible only to the parent runtime after the tool call,
not to the model while composing its response. It is therefore audit metadata today rather than a
claim reference the model can proactively emit. A future tool surface may expose a bounded ID back
to the model without exposing the result body.

### Option generation may carry explicit claims

`GeneratedOptions` gains an optional `claims` collection using `RationaleClaim`. Action-only option
sets remain valid with no claims. If the chair makes explicit supporting assertions, they use the
same normalisation and frozen-reference checks as later phases.

### Existing terminal status remains two-layered

`completion.json.status=complete` continues to mean that the run bundle was durably completed;
`DecisionReport.status` remains the public deliberation outcome. This change fixes contribution
ingestion rather than breaking the persisted completion schema.

## Risks / Trade-offs

- [A cited URL may be hallucinated] → Store it as a declared public citation, never as a verified
  artifact; retain strict opaque artifact validation.
- [Automatic downgrade may hide weak support] → Record it in the private audit trail and keep the
  claim's basis as `inferred` in the accepted message and persisted decision.
- [New model fields change content hashes] → Omit empty citation fields during serialization so
  existing bundles retain their original report and completion hashes while new runs can use them.
- [Bridge result metadata could leak information] → Persist only fixed metadata and hashes, never
  arguments or result bodies.
- [Prompt compliance can still vary] → Replay the observed failure payloads in deterministic tests
  and leave all non-recoverable validation fail closed.

## Migration Plan

1. Add models and normalisation with backward-compatible defaults.
2. Extend the NDJSON bridge and runtime accumulator with optional artifact metadata.
3. Add prompt instructions and option-generation claim validation.
4. Run historical-shape, orchestrator, bridge, TUI, and full regression tests.
5. Reinstall the local package and run zero-model checks before requesting a paid real meeting.

Rollback is a code/package rollback; existing persisted runs require no migration.

## Open Questions

- A future Hermes/Codex integration may expose verifiable Codex-native web artifacts. When it does,
  those records can move from `source_urls` to opaque verified artifact IDs without changing this
  distinction.
