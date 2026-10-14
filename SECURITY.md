# Security

Switchyard 0.3 is an alpha for a single user's local Mac. Automated security and fault tests improve confidence; they are not an independent penetration test or a security guarantee.

## Boundaries

- The service binds to IPv4 loopback. All API/control routes except minimal `/health` require a local bearer token or `x-api-key`. Host and Origin checks reject other hosts and cross-origin callers; there is no browser dashboard or permissive CORS policy.
- Token/config/database files are private to the current user. Symlinks, hardlinks, public token permissions, empty tokens, and unsafe data directories fail closed. The token grants full local control; separate scopes are not implemented.
- Other processes running as the same OS user can generally read that user's files or interact with their runtimes. Switchyard is not a sandbox against a compromised account, malicious administrator, compromised runtime, or hostile model executable.
- Runtime connections must use literal loopback addresses. Redirects and proxy-environment routing are disabled. Known remote/cloud aliases are excluded. The user must disable cloud forwarding in each runtime; Switchyard cannot prove a separate application's egress policy. No packet-capture egress certification is claimed.
- Requests, JSON complexity, schemas, upstream output, concurrency, waiting, downloads, retained approvals, and decision history have finite bounds. Sensitive-data detection is a heuristic. Review the entire outgoing payload; detection is not proof that data is safe.
- Cloud sends require exact payload/provider/model approval and a Keychain credential. Sensitive tags block sending. Duplicate concurrent approvals produce one send; failures are not automatically replayed. Response data remains in bounded temporary memory until queue expiry/eviction; raw prompts and responses are not written to the decision journal.
- Metadata can still be sensitive: model names, timing, routing rules, policy fingerprints, outcomes, and privacy tags appear in authenticated status and exports. Review exports before sharing.
- The journal uses APSW with SQLite 3.54.0, disabled extension loading, defensive mode, and untrusted-schema handling. The bundled Python distribution contains its older stdlib SQLite library; the app bootstrap blocks importing that entry point. It is not used for the journal. This does not make arbitrary scripts run with the included interpreter safe.
- Memory reservations are estimates, not enforcement of OS/GPU allocations. External clients can race runtime polling. The router protects visible active/resident/external allocations and blocks uncertain state, but cannot promise zero swap or prevent every runtime allocation spike.

## Reporting

Use the repository's **Security → Report a vulnerability** option when available. Do not post credentials, private prompts, or exploit details in a public issue. If private reporting is unavailable, open an issue requesting a private reporting channel without the sensitive details.

Include version, runtime version, a minimal synthetic reproduction, expected result, and observed result. No response-time SLA is offered for this alpha. Reproduce against a disposable profile and simulated runtimes where possible.

## Known release limits

The app is ad-hoc signed and not Apple-notarized. No updater, telemetry, automatic cloud fallback, or background login-item installation is included. Live LM Studio inference, real paid-provider sends, older macOS versions, large-model thermal/pressure soak, and PRD E1–E6 certification remain unverified. Dependency checks cover package advisories at a point in time, not all possible vulnerabilities in bundled native libraries or macOS.
