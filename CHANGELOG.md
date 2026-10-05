# Changelog

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
