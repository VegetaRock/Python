# Validation performed — v4

## Executed successfully

- Python syntax compilation of the application, integration helper and tests.
- **84 unittest tests passed**, with fake cursors/connections.
- **GUI smoke test passed** under Xvfb with actual Tk widgets and mocked services.
- Apply and Approve screenshots were captured and visually inspected.

The automated checks cover the shared singleton and field mapping, session/role
changes, transaction lifecycle, query placeholders/parameters, SQL date/status/
scope/concurrency predicates, status filtering before the history limit, own-row
visibility, explicit self-action policy, approved cancellation and required
reason, original-approval preservation, audit atomicity on a simulated failure,
3 PM–11 PM boundaries, ten-minute increments and legacy-record handling.

GUI checks exercise both sets of three dropdowns, fixed PM, hour 11 restricting
minutes to 00, live duration, apply/approve filters, selection button states,
submit/edit/pending-cancel/approved-cancel interactions, own visibility and default
self-action restrictions, explicit self-action opt-in, employee access and the
read-only employee identity fields. Previews contain synthetic demo data.

## Not executed / limitations

No real PostgreSQL server or the user's actual pg_util implementation was
available. The SQL migration/trigger has not been executed on a PostgreSQL
instance. Unit tests inspect SQL and simulate results; they do not prove SQL
execution, real row-lock concurrency, database privileges or timezone settings.
Actual Windows DPI/theme rendering was not tested; GUI rendering was checked
under Linux/Xvfb.

Run the included scripts against a staging database and test actual permission,
duplicate, concurrent update, cancellation rollback and midnight cases before
production deployment. No production data was accessed or changed.
