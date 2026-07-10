# Cognee Memory Service

`./run.sh up` starts the authenticated Cognee REST API on `127.0.0.1:8890` with
per-user, per-dataset isolation. Secrets are loaded from the repository-root `.env`.
Cognee's internal extraction and embedding calls use the authenticated GitHub Models API;
this is Cognee's model substrate, not a replacement for RocketRide's orchestration role.

The secure default uses Cognee's embedded Kuzu graph backend. Cognee's current access-control
matrix does not support self-hosted Neo4j for multi-user dataset isolation; only its Aura
dataset handler supports that combination. `./run.sh neo4j-probe` preserves the design's
local-Neo4j fallback experiment with authentication enabled but backend ACL disabled. The
code knowledge graph continues to use the dedicated `ripple-neo4j` service in every mode.

No service port binds to `0.0.0.0`. Stop the stack with `./run.sh down`.
`ACCEPT_LOCAL_FILE_PATH` is enabled because Cognee stages authenticated multipart uploads as
container-local temporary files before ingestion. The container has no host source mount;
only Cognee's own state volume is available, and the API is key-protected and loopback-only.
