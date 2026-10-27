# Overtime Management — your SharedVariable integration

A Tkinter Toplevel for the host application's existing authenticated session and
PostgreSQL connection helper. The left sidebar contains Apply and Approve; the
employee enters only OT start time, end time and reason.

**Keep your existing `shared_var.py` and `pg_util.py` unchanged.** This package
intentionally does not include replacement versions of them.

This revision changes the Python integration only. There is no database schema
change from the preceding shared-variable package.

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

## 2. Exact mapping for your shared_var.py

Your module creates one existing instance:

```python
shared_variable = SharedVariable()
```

The application now reads:

| Field | Existing value |
| --- | --- |
| Employee ID | `shared_var.shared_variable.user_emp_id` |
| Employee name | `shared_var.shared_variable.user_name` |
| Department | `shared_var.shared_variable.department` |
| Role snapshot (session checks only) | `shared_var.shared_variable.role` |

The fields `initial`, `theme`, `shift`, `holidays`, `lnk_path` and `path` are
left untouched. No shift, holiday or theme rules are inferred from their names.
Employee ID/name/department must be populated by your login process, not left at
None. A useful error is shown if they are missing or empty.

Open the window from your existing Tkinter application:

```python
from tkinter import messagebox
from overtime_management import OvertimeWindow


def open_ot_window():
    try:
        # Imports shared_var and uses its existing shared_variable automatically.
        return OvertimeWindow(master=root)
    except (ValueError, RuntimeError, ImportError) as exc:
        messagebox.showerror("Cannot open OT", str(exc), parent=root)
```

Here `root` is your application's existing root/parent widget. In a Tkinter
window class, use `master=self` when self is that widget. No second Tk root or
mainloop is created by the OT module.

Explicit module and instance forms are also supported:

```python
import shared_var
window = OvertimeWindow(master=root, shared_vars=shared_var)

# Alternative: passing the existing instance directly also works.
from shared_var import shared_variable
window = OvertimeWindow(master=root, shared_vars=shared_variable)
```

Use only one of those forms at a time. The default/module form is preferred
because it also notices when login code replaces the singleton itself. Never
instantiate `SharedVariable()` in the OT code; that would create a separate,
empty object instead of using the active login session.

Default field mapping (already configured in the downloaded Python file):

```python
SHARED_FIELD_MAP = {
    "employee_id": "user_emp_id",
    "employee_name": "user_name",
    "department": "department",
}
```

The helper supports ordinary values and Tkinter variables. Employee identity is
snapshotted when the window opens. Before UI operations it checks that the
current employee and role have not changed. Close all OT windows on logout or
permission revocation; create fresh ones after the next login. Role checks only
notice the role value, not external permission changes that leave it unchanged.

## 3. Approver access — do not guess your role values

The actual values used by your `role` field were not supplied. Therefore a
nonempty role, or a value such as "Admin", does NOT automatically enable
approval. The default is `is_approver=False`.

Pass a boolean from your existing trusted permission check:

```python
window = OvertimeWindow(
    master=root,
    is_approver=can_approve_ot,  # Actual bool from your authentication/permissions.
    approval_departments=["Production", "Maintenance"],
)
```

When `is_approver=True` and `approval_departments` is omitted, the scope is the
logged-in employee's department. Explicit department names must match stored
strings. An empty list denies approval. Self-approval is excluded.

`example_integration.py` includes a reusable button handler. Its
`OT_APPROVER_ROLES` allowlist is deliberately empty. Populate it with the exact
role strings your application authorizes for OT, or supply your own
`permission_check` callback. Permission is rechecked whenever the button opens
or reuses a window. Never use `bool(shared_variable.role)` as a permission check:
it would grant approval to every nonempty role, including ordinary employees.

This is department-scoped approval, not a reporting-manager hierarchy. No
manager-to-employee mapping was provided.

### Security boundary

Read-only fields, shared variables and a Python boolean are not independent
proof of identity. This module is intended to integrate with a trusted
authenticated host application. Service methods repeat checks and add employee,
department, date, status and optimistic-concurrency predicates to their SQL,
but a modified desktop client with direct table-write credentials could bypass
that code.

For production with untrusted clients, enforce authentication and approval
permissions through a trusted backend or database-authenticated identities with
properly restricted stored procedures/RLS. Do not expose unrestricted table-write
credentials. The included schema does not provide database-enforced identity,
role authorization, immutable audits or protection from arbitrary direct SQL.

## 4. Employee workflow

1. Open Apply. Employee details are filled from shared_var.shared_variable and are read-only.
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
They test exact field mapping, singleton resolution, identity/role changes,
Tkinter variables, time validation, SQL safeguards, authorization gating and
transaction cleanup. A GUI smoke check is also provided:

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
