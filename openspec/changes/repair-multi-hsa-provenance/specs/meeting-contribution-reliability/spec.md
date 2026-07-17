## ADDED Requirements

### Requirement: Recoverable formatting errors do not erase member contributions
The orchestrator SHALL accept a contribution after deterministic evidence normalisation when the
remaining payload satisfies the response model and frozen decision references. It SHALL NOT make a
second paid model call solely to repair a recoverable provenance-shape error.

#### Scenario: Mixed valid and recoverable member outputs
- **WHEN** selected members return valid contributions or contributions requiring only deterministic provenance normalisation
- **THEN** every such member is recorded as successful and participates in quorum and aggregation

### Requirement: Unrecoverable references remain fail closed
The orchestrator SHALL continue to reject unknown option, criterion, principle, evidence, memory,
and opaque tool artifact references after normalisation.

#### Scenario: Unknown frozen reference remains
- **WHEN** a contribution contains a reference that cannot be proven from the frozen run or current runtime response
- **THEN** that member is excluded from quorum and the audit trace records the failure

### Requirement: User-facing meetings prioritise deliberation content
The TUI SHALL render every accepted member's position, reasons, concerns, and next actions, including
contributions accepted after internal normalisation. It SHALL NOT display confidence scores,
provenance validation diagnostics, or agent-validation metadata in the normal conversation view.

#### Scenario: Normalised contribution is accepted
- **WHEN** a member contribution is accepted after internal normalisation
- **THEN** the TUI displays it exactly like any other accepted contribution without exposing the normalisation diagnostic

#### Scenario: A member irrecoverably fails
- **WHEN** a member has no usable contribution after validation
- **THEN** the TUI gives a short unavailable state while the final decision accurately reflects the reduced quorum

### Requirement: Regression tests cover real multi-member failure shapes
The test suite SHALL cover public URLs placed in artifact IDs, grounded claims without references,
Hermes tool artifact propagation, mixed member outcomes, and a multi-member meeting in which all
recoverable contributions are counted.

#### Scenario: Historical output shapes are replayed
- **WHEN** tests replay the two provenance shapes observed in persisted real runs
- **THEN** recoverable contributions pass while fabricated opaque references still fail
