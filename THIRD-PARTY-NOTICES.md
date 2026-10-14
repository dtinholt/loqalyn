# Third-party components

Loqalyn's original application source and C launcher are MIT-licensed. Model weights and inference runtimes are not distributed with the app. Router projects cited in the research are references, not incorporated dependencies.

The Mac bundle contains Astral's python-build-standalone distribution of CPython 3.12.15 and the 26 packages listed with versions, URLs, and SHA-256 hashes in `macos/dependencies.lock.json`. CPython and its embedded libraries retain their upstream notices in `macos/licenses` and in the bundle's `Contents/Resources/licenses`; Python's own license is also retained in the runtime. Some notices cover components/options not active in this particular binary.

Python wheel license/notice files are retained inside `Contents/Resources/python-packages/*.dist-info`. They include aiohttp and its llhttp vendor notice, PyObjC, APSW, PyYAML, jsonschema, certifi, and their supporting packages. The MIT license for Loqalyn does not override these upstream licenses. APSW's bundled SQLite is public domain; CPython and OpenSSL retain their own terms.

Exact upstream source and wheel download locations are available in the lockfile. The separately downloadable Qwen3 0.6B model used for local testing is not bundled or published in these release artifacts.
