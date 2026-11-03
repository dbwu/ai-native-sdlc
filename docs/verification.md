# Verification

Verified in this workspace using Python 3.14.0 and a temporary native PostgreSQL
17.11 instance (portable binaries from [EDB](https://www.enterprisedb.com/download-postgresql-binaries)).

- `python -m pytest -q`: **65 passed**, including 36 PostgreSQL integration cases.
  The only warning is a dependency deprecation notice about the test client's
  use of httpx; it does not affect test results.
- Real Uvicorn HTTP smoke: health, interactive docs, metamodel, synthetic demo,
  entity queries, and relationship queries passed. The demo created 15 entities
  spanning all 12 kinds and 9 links spanning all 9 relationship types.
- Migration command/replay passed against PostgreSQL. Tests also check database
  immutability, failed-write rollback, and concurrent graph/revision writes.
- Wheel build passed; the wheel contains the SQL migration alongside the Python
  package. Runtime and test dependency resolutions are recorded in the lock files.

Docker is not installed in this environment, so the Compose image build/run was
not executed. The API, migration, demo, and database behavior were verified natively.
Python 3.11/3.12 and other PostgreSQL major versions were not exercised here.

Temporary binaries, database files, package caches, and smoke artifacts are under
ignored local directories and are not part of the implementation source.
