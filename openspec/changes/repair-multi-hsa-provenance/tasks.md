## 1. Evidence Model and Normalisation

- [x] 1.1 Add validated public citation URLs and optional option-generation claims
- [x] 1.2 Implement deterministic provenance normalisation with private audit metadata
- [x] 1.3 Keep unknown opaque and frozen references fail closed

## 2. Hermes Bridge Contract

- [x] 2.1 Emit sanitised artifact metadata for completed Hermes-native tools
- [x] 2.2 Parse optional bridge artifacts into `RawAgentResponse.tool_artifacts`
- [x] 2.3 Verify backward compatibility and sensitive-data exclusion

## 3. Prompting and Meeting Projection

- [x] 3.1 Explain citation, opaque artifact, and inference rules in the task prompt
- [x] 3.2 Apply claim validation consistently to option generation and formal phases
- [x] 3.3 Ensure the TUI shows accepted deliberation content without validation diagnostics

## 4. Regression Coverage

- [x] 4.1 Replay URL-as-artifact and missing-provenance real-run shapes
- [x] 4.2 Test bridge tool artifact propagation and older bridge responses
- [x] 4.3 Test multi-member quorum with all recoverable contributions counted
- [x] 4.4 Run targeted tests, full tests, Ruff, and strict OpenSpec validation

## 5. Local Delivery

- [x] 5.1 Reinstall the local HSA package without Docker
- [x] 5.2 Run zero-model CLI, profile, and bridge readiness checks
- [ ] 5.3 Run a real multi-HSA smoke test only after explicit cost confirmation
