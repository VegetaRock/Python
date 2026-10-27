# Validation of the SharedVariable integration update

## Executed successfully in this environment

- `python -m unittest discover -s tests -v`: 54 tests passed.
- `xvfb-run -a python tests/gui_smoke.py`: passed on Tk 8.6 with mocked service responses.
- `python -m compileall -q .`: passed.
- `schema.sql` is byte-for-byte identical to the prior shared-variable package.

The unit tests cover exact user_emp_id/user_name/department mapping, existing
shared_variable singleton resolution, module or instance input, identity and role
changes, unpopulated login fields, Tk variables, time/reason validation,
application-side authorization predicates and mocked transaction cleanup.

The GUI smoke test covers creation from the default shared_var import and a
passed instance, three read-only employee fields, blank initial OT times,
duration preview, submit/edit/cancel, approval, request details, employee/role
changes and the disabled Approve button for an ordinary employee. Apply and
Approve screenshots were generated and visually checked during this run.

## Not tested here

No connection was made to the user's PostgreSQL server. The real pg_util module,
login/authentication system, permission model, network performance and Windows
Tk installation were not available for integration testing. SQL tests use fake
cursor responses; they are not live execution of SQL against PostgreSQL.

Use a test database before deployment. This package does not claim server-side
authorization or database-enforced audit immutability.
