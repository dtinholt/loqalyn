# Build and release

Use an Apple silicon Mac, Python 3.12+, Apple's command line tools, and network access for the pinned upstream artifacts. No original app archive or developer account is needed for an ad-hoc build.

```sh
python3 macos/build_app.py --output build/release --cache build/downloads
build/release/Loqalyn.app/Contents/MacOS/Loqalyn --cli --help
codesign --verify --deep --strict build/release/Loqalyn.app
python3 scripts/package_app.py build/release/Loqalyn.app dist/Loqalyn-0.3.0-alpha.3-Mac-Apple-Silicon.zip
```

The builder verifies SHA-256 hashes before extraction, bundles Python 3.12.15 and the exact macOS wheels in `macos/dependencies.lock.json`, compiles the full C launcher, signs nested binaries, and verifies the app. It excludes development-only PyObjC bundles and unused pip installation tooling. Runtime and package notices are preserved. `--base-bundle` is a development shortcut; published builds use normal downloads.

The dependency inputs are reproducibly identified; the app is not claimed byte-for-byte reproducible because code-signature/build metadata can vary. Build into a fresh directory. Source-tree changes after signing require a rebuild. Do not write caches or support data into the app bundle. Finder metadata/resource forks may invalidate strict signature checks: the packaging script omits extended attributes, preserves executable permissions/symlinks, and validates a clean extraction.

The release is **ad-hoc signed**, not Developer ID signed or notarized. A maintainer with Apple credentials must replace ad-hoc signing with a tested hardened-runtime/entitlements configuration, sign all nested executables with Developer ID, notarize, staple, and verify Gatekeeper behavior before claiming notarized distribution. Credentials are intentionally absent from the repository and CI.

Before release, run unit/integration tests, the synthetic stress harness, native launch/interaction checks, actual-model smoke tests, a dependency advisory query, and a secret scan. Publish the evidence scope and limitations with the artifacts and SHA256SUMS. Never package local runtime profiles, model weights, tokens, Keychain data, or raw user logs.

For upgrades, quit Loqalyn, back up its private data folder to a private destination, replace the app, and reopen. Existing invalid/unsafe configuration fails closed with a visible error. There is no automatic migration/rollback system in this alpha; keep the previous app and profile backup until the upgrade is validated.

`ci/validate.yml` is an optional GitHub Actions template for Linux gateway tests and Apple silicon/macOS bridge tests. To activate it, copy it to `.github/workflows/validate.yml` with an identity authorized to manage workflows. The current release's evidence comes from local execution; hosted CI is not claimed as passed.
