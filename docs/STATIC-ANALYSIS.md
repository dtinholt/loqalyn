# Static analysis review

Bandit 1.9.4 inspected the application, macOS bootstrap/builder and support scripts with no scan errors and no suppressed checks. The [raw report](evidence/static-analysis.json) contains **38 findings: 1 high, 5 medium and 32 low**. A scanner's severity is a review cue, not evidence that the code is exploitable. The findings were reviewed as follows; no confirmed high/medium exploitable issue was identified within the documented single-user threat model.

| Rule | Count | Review |
| --- | ---: | --- |
| B202 archive extraction | 1 high | The flagged receiver is `zipfile.ZipFile`, although the diagnostic says tarfile. `unpack_wheel` first rejects absolute paths, parent traversal and oversized members. Only SHA-256-verified, explicitly locked wheels are accepted. Runtime tar extraction separately uses Python's `filter='data'`. This is not an unauthenticated archive-upload path. |
| B310 URL opening | 2 medium | Builder URLs come from the reviewed source-controlled hash lock; advisory queries use a fixed HTTPS PyPI origin. These maintainer scripts must not be run with an untrusted replacement lock. Runtime request destinations have their own strict loopback/provider checks. |
| B608 interpolated SQL | 3 medium | Interpolation is limited to the internal constant table names `decisions` and `events`. Request values never select an SQL identifier; data, row limits and cutoffs use bound parameters. |
| B404 subprocess imports | 4 low | Imports support local build tools, the isolated stress subprocess and physical-memory detection. Importing the module does not execute a request-supplied command. |
| B603 subprocess calls | 13 low | Argument arrays are used without a shell. Builder/packager inputs are maintainer-selected paths; runtime memory detection uses fixed arguments. The stress process launches the current Python interpreter with an owned disposable profile. |
| B607 executable search path | 11 low | Apple build/signing tools and the `sysctl` fallback rely on the developer/user's command search path. A compromised same-user PATH is outside the local-account security boundary; builds require a trusted host/environment. The app's compiled launcher resolves bundled Python relative to itself and does not search PATH. |
| B110 ignored exceptions | 3 low | Optional crash-alert fallback, missing optional Keychain runtime credentials, and transient stress sampler errors. Startup still exits on failure; tests independently require valid samples, budget limits and successful recovery. |
| B105 empty password literal | 1 low | An empty initial controller token is a sentinel before securely loading/creating the actual random token. It is not an accepted credential. |

These dispositions do not prove absence of vulnerabilities. Review also included the C launcher's bounded path construction, isolated Python invocation and argument allocation. Dynamic adversarial tests cover the exposed HTTP/MCP, runtime, file, database and approval boundaries described in [VALIDATION.md](VALIDATION.md).
