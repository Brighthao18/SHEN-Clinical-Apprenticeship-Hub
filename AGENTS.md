# Project operating rules

Read this file and the current documents under `docs/` before changing architecture or clinical data semantics. Work within the requested scope, preserve unrelated files, and complete appropriate verification. Use Windows-compatible paths and UTF-8.

## Product boundary

SHEN is a local-first single-user Chinese medicine apprenticeship knowledge workspace. The primary flow is upload -> immutable source -> OCR -> strict clinical extraction -> validation and deterministic resolution -> imported encounter or unresolved ingestion. Do not add multi-user behavior, HIS/FHIR, new hospital parsing families, RAG, or teacher-pattern claims without a separately documented decision.

## Data invariants

- Preserve source bytes, SHA-256, OCR text, and complete provider output. Never overwrite source evidence.
- Keep PatientIdentity PII separate from clinical Patient data. Raw-image OCR, identifiable OCR-text extraction, and Vision Verification each require their existing explicit per-service consent. Analysis and writing consume only de-identified contexts. All live services default off.
- Never invent missing facts. Missing is NULL/not recorded, not normal, stable, or resolved.
- Preserve provenance, including physician_record, student_note, ai_extracted/ai_normalized, ai_inferred, external_reference, and source-specific visual verification. AI inference never becomes a physician diagnosis or an unrecorded teacher quotation.
- Match patients deterministically by hospital + medical record number fingerprint; ambiguous fuzzy matches fail closed. Encounter age belongs to the encounter. One Encounter may have many Documents.
- Keep source units; normalize 包/袋 to package only for analysis. Existing JNU semantics interpret `(10g)2包` as 20 g per decoction. Course totals never replace per-decoction dose. The legacy 诊号 is a visit serial, not an MRN, and masked numbers never establish identity.
- Hospital specifications are user-entered, manual_verified and versioned. Unknown specifications never yield guessed grams.
- Validate before any patient/encounter write. UNRESOLVED retains source, OCR and diagnostics, but commits no Patient/Encounter/Prescription rows. Reprocessing is an explicit command, dry-run before apply.
- Traceable unmapped drug names remain pending with canonical NULL. Untraceable drug identity blocks. AI suggestions never establish drug or patient identity.
- Ingestion, Vision Verification, deterministic Prescription Diff, longitudinal contexts, and writing evidence rules are frozen. Do not change their behavior for packaging or to improve apparent coverage.
- Diff uses verified canonical identity first, unique exact raw-name fallback for pending identity, and only per-decoction calculated_dose_g. Unknown dose stays UNKNOWN. Processing change is independent. Ambiguous duplicates fail closed.
- Longitudinal AI and writing cite validated evidence keys and selected Encounter IDs. Prior inference is never promoted to source fact. Writing versions are append-only: regeneration and edits never overwrite source text, old drafts, or final versions.

## Engineering and public release

- PostgreSQL/SQLAlchemy/Alembic is the source of truth; review migrations. Keep provider adapters and idempotent stages.
- Authenticate state changes and clinical reads. Uploaded files are never public static assets. Logs contain stable IDs, not identity fields or OCR text.
- Real clinical data belongs only in an isolated production profile with user-created secrets. Never use it in development, CI, fixtures, artifacts or image layers. Never scan private directories or bulk-submit them to a provider.
- Use only from-scratch synthetic fixtures in public work. No actual records, private keys, hospital specification exports, private reports or old private Git history may enter releases.
- Bind Web/API to localhost; database and queue stay Docker-internal. Preserve backup manifests, hashes, and isolated restore verification. Core clinical data is never housekeeping material.
- The demo has its own project, port and volumes. Backend services use only an internal network; Web also has a localhost entry network. Its fixed credentials must never become production credentials.
- Use the existing fail-closed E2E lock. An existing lock, including a dead/corrupt/unknown owner, always refuses. Never auto-reclaim it or kill a process merely because it holds a port.
- Record business ambiguity in `docs/OPEN_DECISIONS.md`. Run backend tests/static checks, frontend lint/types/tests/build, isolated PostgreSQL migration checks, E2E and relevant container checks in proportion to changes. Report limitations honestly.
