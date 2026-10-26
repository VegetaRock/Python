# Validation performed

- Python syntax compilation passed for application, example and tests.
- 41 unit tests passed (`python -m unittest discover -s tests -v`).
- GUI smoke test passed under a Linux Xvfb display.
- Apply and Approve layout screenshots were inspected with demo data.

The unit tests use fake PostgreSQL cursors/connections. The GUI smoke test uses
in-memory service responses. These checks do not establish that the SQL has run
against a live PostgreSQL server or that the user's own pg_util is compatible.
No connection to the user's database was attempted.

Before deployment, test database setup, real commit/rollback/release behavior,
role/scope integration, duplicate requests, midnight rollover and simultaneous
edits/approvals with the actual host application and a test PostgreSQL database.
