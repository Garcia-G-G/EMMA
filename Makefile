# Emma — build targets.
.PHONY: pkg sign-and-notarize test lint type

# Build the UNSIGNED installer .pkg (Prompt 29.0). Output: dist/Emma.pkg
pkg:
	./installer/macos_pkg/build_pkg.sh

# Sign + notarize a previously-built dist/Emma.pkg (Prompt 29.1, deferred).
# Requires Apple Developer certs + env vars — see installer/BUILD.md. NOT run by `pkg`.
sign-and-notarize:
	./installer/sign_and_notarize.sh

test:
	.venv/bin/python -m pytest tests/ -q

lint:
	.venv/bin/ruff check .

type:
	.venv/bin/mypy .
