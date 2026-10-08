# Project rules
- This is a Falcon-style transport (SIGCOMM 2025 paper; DOI in README — the PDF is not committed,
  download it to docs/falcon-paper.pdf locally to cite sections). Never call it "Falcon" or claim parity with
  the paper's numbers.
- Protocol code (core logic in pdl/, tl/, fae/, baselines/) is pure: no I/O, no wall-clock, no global RNG. Time
  and RNG are injected.
- Every state change emits an event conforming to docs/event-schema.md.
- Mechanism lives in pdl/ and tl/. Policy (CC, timeouts, path choice, DT alpha) lives only in fae/.
- Same seed must give identical event logs. Add a test when adding randomness.
- When implementing a paper feature, cite the section in the docstring. If you simplify something, add it to
  docs/feature-matrix.md.
- Write tests with each feature. Run pytest before declaring a task done.
- Ask before adding dependencies.
