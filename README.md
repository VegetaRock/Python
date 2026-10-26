# Overtime Management — shared_var integration

A Tkinter `Toplevel` with the two-button Apply / Approve sidebar from the supplied
layout. The host application owns login, `shared_var`, `pg_util`, the Tk root,
and approval authorization. Python 3.10 or newer is required.

## What changed

Employee ID, name and department are read from your existing `shared_var`
module/object when the OT window opens. They are displayed as **read-only**
fields; employees enter only **start time, end time and reason**. Start and end
are blank initially so the employee must deliberately enter the times.

Only `shared_var.name` was explicitly specified. This version assumes the other
attributes are `shared_var.employee_id` and `shared_var.department`; change the
mapping below to match your actual variable names.

Plain Python values and Tkinter variables (such as `tk.StringVar`) are supported.
The helper reads `.get()` for Tkinter variables on the UI thread. No employee
master table or separate login form is needed for this integration.

## 1. Files and database setup

Copy `overtime_management.py` into an importable location beside your existing
application. Keep using your own `shared_var.py` and `pg_util.py`; neither is
bundled, so this project cannot overwrite your login data or database credentials.

Run `schema.sql` in the same PostgreSQL database used by `pg_util`, using your
normal schema-owner/migration account. If you already installed the previous
version's schema, the same tables are compatible; the script also adds two
lookup indexes. It uses `CREATE ... IF NOT EXISTS`, does not drop tables, and does
not rewrite existing request data. This assumes the tables are those from the
previous download; it is not a migration for unrelated tables with the same names.

Use the same PostgreSQL schema/search_path for setup and application connections.
The application needs SELECT/INSERT/UPDATE on `ot_requests`, SELECT/INSERT on
`ot_audit`, and sequence permissions for the serial ID columns. Do not use a
superuser or schema-owner account for ordinary runtime access.

`pg_util.get_connection()` must return a psycopg2-compatible, **idle**, exclusively
borrowed connection. `pg_util.release_connection(conn)` must return it to the
pool or close it, and must discard broken connections as appropriate. A global
connection simultaneously used by other operations is not suitable.

If your existing pg_util already works, no extra database package is required
by this module. Install the driver your pg_util imports if it is not installed;
for a psycopg2-based implementation, that can be `pip install psycopg2-binary`.

## 2. Open it from your existing Tkinter application

Your authenticated login process should already have populated the real values:

```python
# Illustrative attribute names, not a new login implementation:
# shared_var.employee_id = the_authenticated_employee_id
# shared_var.name = the_authenticated_employee_name
# shared_var.department = the_authenticated_department
```

```python
import shared_var
from overtime_management import OvertimeWindow

# root is your EXISTING Tk root, Toplevel, or parent widget.
window = OvertimeWindow(root, shared_vars=shared_var)
```

Approval is disabled by default. No second `tk.Tk()` or `mainloop()` is created.
You can also use `OvertimeWindow(root)` when `shared_var` is directly importable.
See `example_integration.py` for a button handler that reuses the open OT window.

### Different shared-variable names

For example, if you use `shared_var.emp_id`, `shared_var.name`, `shared_var.dept`:

```python
window = OvertimeWindow(
    root,
    shared_vars=shared_var,
    field_map={
        "employee_id": "emp_id",
        "employee_name": "name",
        "department": "dept",
    },
)
```

The dictionary keys are fixed logical field names. The dictionary values are the
attribute names in your shared_var module. You can instead change
`SHARED_FIELD_MAP` once at the top of `overtime_management.py`.

Employee identity is snapshotted at window creation, not taken from text fields
when saving. The window checks for a changed shared identity before operations.
Close OT windows on logout/user change; reopen them after a new login. Also close
and reopen them when approval permissions change, because those are supplied by
the host application at window creation.

## 3. Approver access

Do not infer approval rights from a person's name or department. Pass a real
boolean from your authenticated application's existing permission check:

```python
window = OvertimeWindow(
    root,
    shared_vars=shared_var,
    is_approver=can_approve_ot,  # bool returned by YOUR trusted permission logic
    approval_departments=["Production", "Maintenance"],
)
```

`can_approve_ot` is an integration placeholder, not an assumed shared_var field.
When `is_approver=True` and `approval_departments` is omitted, approval is limited
to the logged-in user's department. Specify an explicit department list for a
cross-department approver. Department names must match stored strings exactly.
An empty list gives no approval access. Self-approval is excluded in the queries.
Do not set `is_approver=True` for every employee.

This version uses **department-scoped** approval, not a reporting-manager
hierarchy. No manager-to-employee mapping was provided. It does not invent one.

### Security boundary

Read-only widgets, shared_var values and a Python boolean are **not** independent
proof of identity. This is an integration for a trusted authenticated host app.
The service repeats role checks and adds ownership, department, date, status and
optimistic-concurrency predicates to its SQL, but a modified desktop client with
direct table-write credentials can bypass this Python service.

Before an adversarial/multi-user production deployment, authenticate users at a
trusted backend, or use database-authenticated identities and properly restricted
stored procedures/RLS. Do not expose unrestricted table-write credentials to
untrusted clients. The schema here does not claim database-enforced identity,
role authorization, audit immutability or protection from arbitrary direct SQL.

## 4. Employee workflow

1. Open Apply. Employee details are filled from shared_var and are read-only.
2. Enter start time and end time in 24-hour HH:MM format; enter a reason.
3. Check the calculated duration and submit.
4. Select today's Pending request to load it into the form, then Save changes
   or Cancel selected. Double-click or View details to read full reason,
   approver remarks and audit entries.

There is no editable application-date field. Each submission and mutation checks
the PostgreSQL server's business date, using `Asia/Kolkata`. A form kept open
across midnight must be refreshed; it is not silently submitted for a new day.
The date in the header is the last server date read, and mutations revalidate it.
Refresh updates the header; it is not a continuously running clock.

## 5. Approval workflow

Open Approve (authorized users only). The list shows today's requests within
assigned departments, excluding the approver's own requests. Review full details,
select a Pending request and approve or reject. Rejection requires a comment.
The status filter can also show Approved, Rejected, Cancelled or All for today.
Previously finalized requests cannot be changed by this interface.

The update includes the `updated_at` value from the loaded request. If an employee
edits the request, or another approver decides it first, the action fails and the
approver must refresh and review the updated request rather than approve stale
information.

## 6. Explicit policy choices

- One request per employee per business date, including Cancelled and Rejected.
  This preserves the previous schema's UNIQUE(employee_id, ot_date) rule.
  Cancellation does NOT allow another request that day. Edit a Pending request
  instead of cancelling it when changing the time or reason.
- OT date is the START date. An end earlier than the start means the following
  day; the UI shows `(+1d)` and asks for confirmation before submit/edit.
  Example: today's 22:00 to tomorrow's 02:00 is four hours of today's OT.
  It must be approved on its start date, before that business date ends.
- Equal start/end times, invalid times, blank reasons and requests over 16 hours
  are rejected. The 16-hour value is only a configurable application policy,
  **not a legal or payroll rule**. Keep the Python limit and SQL CHECK consistent
  if changing it; changing the Python constant alone does not alter an existing
  database constraint.
- Reasons and decision comments are limited to 1,000 characters in the app.
- Only today's Pending request can be edited, cancelled, approved or rejected.
  Old Pending requests remain Pending in history, but cannot be acted on; they
  are not automatically relabelled Expired.
- Employee history shows the latest 200 requests. Approval lists cover today.
- Duration is elapsed wall-clock minutes; no breaks, payroll rates, attendance,
  shift rules, holidays, minimum increments or legal entitlement are inferred.
- Audit rows and the corresponding request changes are committed together. The
  transaction is rolled back on failure; no hard-delete feature is provided.

## 7. Connection handling and responsiveness

Connections are acquired and released through the two functions you specified.
Autocommit is temporarily disabled and restored; transaction-local timezone,
statement timeout (15 seconds) and lock timeout (5 seconds) are applied.
The connect timeout itself must be configured in pg_util.

Database work currently runs on the Tk/UI thread to stay compatible with an
unknown existing connection pool. A slow connection/query can briefly block the
interface. Do not move calls to workers without also using a thread-safe pool
and dispatching all Tk operations back to the UI thread.

## 8. Tests

From this folder:

```text
python -m unittest discover -s tests -v
```

The included tests use fake cursors/connections, not your live PostgreSQL server.
They test identity mapping, Tkinter variables, time validation, SQL safeguards,
authorization gating and transaction cleanup. A GUI smoke check is also provided:

```text
python tests/gui_smoke.py
```

It uses demo in-memory service responses, opens/validates the GUI and exits. It
does not connect to PostgreSQL or represent a production login.

Before rollout, test with your actual pg_util and a test database: submit/edit,
reject with/without comments, self-approval exclusion, department scope,
permissions, duplicate submissions, midnight rollover and concurrent decisions.
This package does not claim live PostgreSQL integration testing in your environment.

## Reference documentation

Python Tkinter: https://docs.python.org/3/library/tkinter.html
PostgreSQL date/time functions: https://www.postgresql.org/docs/current/functions-datetime.html
Psycopg connection transactions: https://www.psycopg.org/docs/connection.html
