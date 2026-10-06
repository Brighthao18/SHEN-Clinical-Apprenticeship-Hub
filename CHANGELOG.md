# Changelog

## Unreleased

- Return 401 instead of 500 when a login username or development password contains non-ASCII characters; credentials are compared as UTF-8 bytes in constant time.
- Require `exp`, `iat` and `sub` on bearer tokens so a token without an expiry is never accepted.
- Validate provider `base_url` by parsed scheme and exact host instead of a string prefix: lookalikes such as `http://localhost.example.com` or `http://localhost@example.com` and URLs with embedded credentials are rejected, and a stored slot that fails the rule resolves as unconfigured.
- Report an unreachable or failing API on the Web login as unavailable (503/502) rather than as a wrong password or an unhandled error, and return a JSON 502 from the backend proxy when the API cannot be reached.
- Add authentication tests and a Web login exchange unit test.
- Override `source-map-js` to 1.2.2 for GHSA-68fv-2mgg-jv7q (production path next -> postcss) and update only that component's inventory, SBOM and license entry.

Clinical ingestion, matching, dose calculations, deterministic prescription comparison and append-only writing behavior are unchanged. No schema migration is added.

## 0.2.0-rc.1 — 2026-10-05

- Prepare a fresh public source tree with Apache-2.0 licensing and FU HAO attribution.
- Add an isolated synthetic demo, truthful per-service privacy documentation, and project-local installation commands.
- Lock Python transitive dependencies with hashes, pin container digests, and use Valkey for deployment queues.
- Make the isolated E2E runner portable while preserving fail-closed ownership and port checks.
- Add CI, release-content checks, dependency inventory and contribution templates.
- Fix an evidenced cross-platform storage-key boundary bug; reject Windows drives and backslashes on POSIX as well as Windows.
- Keep frontend timestamp tests independent of the host timezone and cover UTC and Asia/Shanghai explicitly.
- Confirm authored synthetic dictionary mappings through the ordinary demo API and isolate backend networking while retaining the localhost browser entry.

Clinical ingestion, matching, dose calculations, deterministic prescription comparison and append-only writing behavior retain their existing semantics. No business schema migration is added. This candidate is an engineering preview, not a clinical-accuracy or production-readiness claim.
