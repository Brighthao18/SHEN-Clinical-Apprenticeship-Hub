# SHEN · Clinical Apprenticeship Hub

SHEN brings clinical records, patient timelines, prescription comparison, and apprenticeship writing into one local-first, single-user workspace for Chinese medicine. It preserves source evidence, compares prescriptions deterministically, and retains append-only apprenticeship writing versions. Open-source preview **v0.2.0-rc.1**, Apache-2.0, copyright FU HAO.

## Try the synthetic demo

Docker Engine 28+ (including Docker Desktop) and Docker Compose v2.24.4+ are required. From the repository root:

Download and extract the source package from [GitHub Releases](https://github.com/Brighthao18/SHEN-Clinical-Apprenticeship-Hub/releases), or clone the repository:

```sh
git clone https://github.com/Brighthao18/SHEN-Clinical-Apprenticeship-Hub.git
cd SHEN-Clinical-Apprenticeship-Hub
```

```sh
docker compose --env-file examples/demo/demo.env -f compose.demo.yaml up --build -d --wait --wait-timeout 180
```

Open <http://localhost:3200>, sign in with `admin` / `shen-demo-only`. Build downloads require Internet access. Backend services use an internal Docker network and mock providers; the Web service additionally has a network for its localhost entry point. Every record and image was created from scratch. Never upload real patient information to this demo: its credentials are public.

The current ingestion semantics are specific to existing JNU outpatient/legacy scenarios. This release does not claim universal hospital support, clinical accuracy, institutional compliance, or multi-user collaboration. Missing clinical facts remain missing. Unresolved documents create no committed patient, encounter, or prescription rows.

Live services are disabled by default. Original-image OCR, identifiable OCR-text extraction, and image verification have separate explicit consent boundaries; longitudinal analysis and writing use de-identified clinical contexts. Other providers and platforms require separate validation.

For production, install Python 3.12 and run `python scripts/shen.py setup`, `init`, then `start` from the root. Initialization creates local secrets and refuses to overwrite existing configuration. Read the [Chinese guide](README.md), [deployment](docs/deployment.md), [privacy boundaries](docs/privacy-boundaries.md), [contribution guide](CONTRIBUTING.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
