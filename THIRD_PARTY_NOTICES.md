# Third-party notices

SHEN original code, project documentation and original synthetic examples are Apache-2.0, copyright 2026 FU HAO. Third-party components retain their own licenses; the root LICENSE does not relicense them. No cloud entitlement, model weight, hospital specification export, institution logo or real clinical record is included.

## Locked dependencies

`licenses/dependency-inventory.json` records exact metadata sources, versions, declared licenses, package integrity and retained license text hashes. `licenses/sbom.cdx.json` is the CycloneDX lock inventory, including development and optional platform packages. The inventory is bound to the three lock-file hashes and includes components not installed on this particular platform.

Python and npm package archives were verified against publisher digests/lock integrity. Original LICENSE/COPYING/NOTICE files are retained under `licenses/pypi/` and `licenses/npm/`. If a package omitted those files, a pinned upstream license source is recorded. Four packages (`@humanfs/types`, `client-only`, `language-subtag-registry`, `stable-hash`) omit a stand-alone notice; their published declaration/README plus a canonical SPDX reference text are retained. This reference is identified as such and no missing copyright author/year is invented.

The application uses lucide-react icons (ISC), Next.js/React (MIT), FastAPI/SQLAlchemy (MIT), Celery (BSD-3-Clause), redis-py (MIT), psycopg/psycopg-binary (LGPL-3.0-only), and cryptography (dual Apache/BSD declarations). The exact inventory and retained texts take precedence over this short overview. Python build tooling is locked too. Ordinary code seed drug names are project seed data requiring clinical curation, not a published hospital specification library.

## Native and container components

Psycopg/psycopg-binary are LGPL-3.0-only; their versioned source is available from https://github.com/psycopg/psycopg and the matching PyPI source distribution. Sharp/libvips components include LGPL-3.0-or-later declarations and bundled libraries with their own licenses. They are not relicensed as Apache. The retained upstream texts and source reference are in the component inventory; source/build details are available from [sharp-libvips](https://github.com/lovell/sharp-libvips). Anyone distributing dependency binaries or built images must preserve notices, provide the applicable LGPL source/replacement rights, and review bundled native libraries. This source package does not include node_modules or container binaries.

Deployment pins Python, Node.js, pgvector/PostgreSQL and Valkey image digests. PostgreSQL/pgvector and Valkey retain their upstream licenses; Node/Python images also contain OS packages with individual licenses. Container distributors must retain those packages' notices/source obligations. Source/build locations: [Python Docker image](https://github.com/docker-library/python), [Node Docker image](https://github.com/nodejs/docker-node), [pgvector](https://github.com/pgvector/pgvector), [Valkey](https://github.com/valkey-io/valkey).

Final application rootfs SBOMs are release verification artifacts, generated with checksum-verified Syft 1.54.0; they include compiled/native/system components beyond lock metadata. Their scope is the final filesystem, not discarded build layers. Build-context and layer isolation are checked separately. Image publication is outside this local source candidate.

## Optional services and tools

MinerU and OpenAI-compatible providers are connection adapters only. Users supply their own credentials and review service/data terms. No model license is conveyed. Restic is an optional separately installed backup tool, not a bundled binary. Package maintainers' notices and any required COPYING files must remain with derived distributions.
