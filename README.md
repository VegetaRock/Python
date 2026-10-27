# Overtime Management v4 — Tkinter + PostgreSQL

A Toplevel that integrates with your existing Tkinter application, authenticated
`shared_var.shared_variable`, and `pg_util` connection functions.

Keep your own `shared_var.py` and `pg_util.py` unchanged. Neither is replaced or
included in this package. No second Tk root, login system or connection pool is
created by the OT module.

## What's changed

| Requirement | Implemented behavior |
| --- | --- |
| See your own OT in Approve | Today's own requests are included, highlighted and marked `(you)`. The selected status filter still applies. |
| Cancel approved OT | A separate **Cancel approved OT** action is available only to an authorized approver for an unchanged Approved request for today in their scope. A reason is mandatory. |
| Apply status filter | All, Pending, Approved, Cancelled, Rejected. Filtering happens in SQL before the 200-row history limit. |
| Three time dropdowns | Each of Start and End has Hour / Minute / PM comboboxes. Hours 3–11; minutes 00, 10, 20, 30, 40, 50; PM fixed. |
| Time window | 3:00 PM through 11:00 PM on the same day, end strictly after start. At hour 11 only minute 00 is offered. |

**Interpretation of own requests:** visibility and permission are separate. Own
requests are now visible to approvers. Self-approval remains disabled by default;
set `allow_self_approval=True` explicitly when your policy permits an approver
to approve, reject, or cancel their own approved OT. That option does not grant
approver status and does not override department scope.

**Minute 60:** it is not a valid clock minute. Select the following hour with
`00` instead: 3:60 PM becomes 4:00 PM. The form never silently rounds existing
stored times, and it never offers 11:10–11:50 PM.

## Install / update

Back up your application files and database using your usual deployment process.
Replace the old `overtime_management.py` with the one in this package. Review
`example_integration.py` when using its button handler.

### Existing OT database

No new columns or tables are required. Cancellation details use the existing
`ot_audit` table, so the new Python code is compatible with the preceding schema.

Run **migration_time_policy.sql** once in your application's database to enforce
the new time window/increments in PostgreSQL as well. This is recommended for
consistent behavior across older clients and direct writes. The Python service
validates the same rules even before this optional database hardening is applied.

Use your migration/schema-owner account and the same schema/search_path used by
`pg_util`. The script is transactional and safe to rerun against the previous
OT schema. It installs one function and trigger, does not delete/rewrite data,
and aborts if its lock wait exceeds five seconds. Retry during a suitable
maintenance window when a lock timeout occurs.

### First installation

Run **schema.sql** once. It creates the existing compatible tables/indexes and
includes the time-policy trigger, so a separate migration run is unnecessary.
The scripts assume these are the tables from this project, not unrelated tables
with the same names.

The retained legacy SQL duration CHECK permits up to 960 minutes so old records
remain compatible. The new trigger limits new or time-edited requests and
transitions into Approved to the requested window (maximum 480 minutes).

### Legacy records

Historical requests outside 3 PM–11 PM or not on a ten-minute boundary remain
visible. The migration does not normalize or delete them. A same-day legacy
Pending request must be edited into the new time policy before approval. It may
still be rejected or cancelled; an already Approved legacy request may be
cancelled by an authorized approver on its business date. Old dates stay
read-only. Unsupported times are not silently loaded as different valid times
into the new dropdowns; inspect the original time in the table or View details.

## Exact existing shared-variable mapping

```python
import shared_var

employee_id = shared_var.shared_variable.user_emp_id
employee_name = shared_var.shared_variable.user_name
department = shared_var.shared_variable.department
role = shared_var.shared_variable.role
```

The role is read for session-change detection only, not automatically interpreted
as approval authority. Your actual role values were not supplied. Use your
existing trusted permission check, or populate the explicit role allowlist in
`example_integration.py` with the exact authorized values.

The fields `initial`, `theme`, `shift`, `holidays`, `lnk_path` and `path` are left
untouched. No extra work-calendar or payroll rules are inferred.

## Open from your application

```python
import shared_var
from overtime_management import OvertimeWindow


def open_ot_window(can_approve_ot: bool):
    # root is your EXISTING Tkinter root/parent.
    # can_approve_ot must come from your authenticated permission logic.
    return OvertimeWindow(
        master=root,
        shared_vars=shared_var,
        is_approver=can_approve_ot,
        approval_departments=None,  # None = the logged-in user's department
        allow_self_approval=False,
    )
```

For employee access only, `OvertimeWindow(master=root)` automatically imports
`shared_var` and resolves the existing `shared_variable` singleton. Passing the
module is preferred over passing the instance because session checks can also
notice when the host replaces the singleton at login/logout.

Do not create another `SharedVariable()` in this module. Your login must fill
`user_emp_id`, `user_name` and `department` before opening the OT window.

For cross-department authority, pass an explicit list such as:

```python
approval_departments=["Production", "Maintenance"]
```

An empty list denies approval access. A string instead of a list is rejected.
When the user is an approver, their own requests are visible even if their own
department is outside that scope, but those requests remain read-only there.

### Enable own approval explicitly

```python
window = OvertimeWindow(
    master=root,
    is_approver=can_approve_ot,  # trusted bool, not bool(shared_variable.role)
    allow_self_approval=True,  # own approve/reject/approved-cancel allowed in scope
)
```

This is a host-configured business policy, not an employee-editable checkbox.
`is_approver=False` always denies the Approve screen and service operations.

### Reuse the same open window

The provided helper avoids duplicates and rechecks permission when reopening:

```python
from example_integration import add_ot_button

button = add_ot_button(
    root,
    permission_check=your_existing_ot_permission_check,
    approval_departments=None,
    allow_self_approval=False,
)
button.pack(padx=10, pady=10)
```

No second root or mainloop is needed. Close all OT windows on logout or permission
revocation. Each guarded UI operation compares current identity and role to its
opening snapshot. External permission changes that leave those values unchanged
are not automatically detected by this desktop module.

## Apply workflow

Employee ID, name and department are read-only. Choose a start/end time and enter
a reason. Each time has three read-only comboboxes, e.g. `[3] [00] [PM]`.
Hour starts blank and minute starts at 00; there is no silently assumed OT slot.
Duration updates as selections change.

Use the status dropdown to show All / Pending / Approved / Cancelled / Rejected.
The current filter persists through refresh and switching views. It is applied
before the latest-200 matching-record limit. A newly saved request may be hidden
by a filter that excludes its status; select All or Pending to see it.

Select today's Pending request to edit or use **Cancel pending**. Save/cancel
buttons are disabled on Approved, Rejected, Cancelled or historical requests.
Employees cannot cancel Approved requests through the Apply screen, even when
the same employee also has approver access; the separate approver operation is
required. Double-click a row or use View details for full reason/history.

## Approval and approved-cancellation workflow

Only authorized approvers can open Approve. The list includes today's requests
in authorized departments plus the approver's own requests. Own rows are marked
`(you)`; the status filter still applies. The default filter is Pending.

Select a Pending row and approve or reject it. Rejection requires a comment.
Own actions require `allow_self_approval=True` and normal department permission.

To cancel an Approved request, choose **Approved** or **All**, select the row,
enter a cancellation reason, then click **Cancel approved OT** and confirm.
Any authorized approver with the required department/self-action permission may
perform this operation; it is not restricted to the original approving person.

The operation changes status to `Cancelled` and updates `updated_at`. It does
not delete the row or overwrite `approver_id`, `approver_comment` or `decided_at`.
An `ot_audit` record with action `CancelledApproved` stores the cancelling actor,
server timestamp and reason. View details shows that record alongside the original
approval. Request update and audit insertion share one transaction; a failure
rolls both back.

The original `Cancelled` audit action continues to mean employee cancellation
of a Pending request. Both kinds of cancellation appear in the Cancelled filter.

## Retained business rules

One request per employee per business date, including Rejected and Cancelled,
remains in force. Cancellation does NOT permit a second submission that day.
Edit a Pending request instead of cancelling it to change its timing/reason.

All mutations are restricted to today's PostgreSQL business date in
`Asia/Kolkata`, not the PC's date. The displayed date is the last server date
read. An apply form left open across midnight must be refreshed before submitting.
Overnight OT is no longer accepted. Historical rows are not automatically marked
Expired; old Pending requests simply remain read-only Pending records.

Approval, edits and both cancellation operations include the loaded `updated_at`
value in the SQL condition. A request changed by another actor must be refreshed
and reviewed again before action. The SQL also rechecks status, current business
date, identity and department/self-action scope as appropriate.

Reasons/comments/cancellation reasons are limited to 1,000 characters in the
application. No payroll rates, attendance rules, shift deductions or statutory
entitlements are calculated. The time window is your requested application
policy, not a legal/payroll assertion.

## Database connection handling and security

```python
conn = pg_util.get_connection()
# Commit successful operations; roll back failed operations.
pg_util.release_connection(conn)
```

Your provider must return an idle, exclusively borrowed, psycopg2-compatible
connection. The module temporarily disables/restores autocommit and sets local
timezone, 15-second statement timeout, and 5-second lock timeout. Configure the
connection timeout and broken-connection disposal in your existing pg_util.

Database calls remain on the UI thread for compatibility with an unknown existing
pool. Slow connections/queries can temporarily pause the UI. Moving them to
workers requires a thread-safe pool and main-thread Tk dispatch; that was not
silently introduced in this revision.

The runtime account needs the existing table/sequence permissions; don't use a
superuser or migration-owner account for ordinary use. A shared variable,
read-only widget and Python boolean are not a server-side identity boundary.
The time-policy trigger validates time values, not who may approve/cancel.
For untrusted desktop clients, enforce authentication and authorization in a
trusted backend or restricted, database-authenticated procedures/RLS. Direct
unrestricted SQL credentials can bypass the application's role, status and audit
rules. This package does not claim immutable audit or database-enforced roles.

## Tests and previews

Python 3.10+ with working Tkinter is the intended runtime. Use the database driver
already required by pg_util; the OT module does not create a new driver dependency.

```text
python -m unittest discover -s tests -v
python tests/gui_smoke.py
```

Linux headless GUI check:

```text
xvfb-run -a python tests/gui_smoke.py
```

The tests use fake database connections/responses, not a real PostgreSQL server.
The GUI smoke check uses demo records and tests widget interaction. Optional
screenshots require Pillow and `OT_SCREENSHOT_DIR`. See TEST_RESULTS.md for
what was actually tested. Test the migration and your real pg_util in a staging
database before production rollout, including simultaneous actions and midnight
rollover. The supplied SQL migration was not executed against your server.

## Reference documentation

- Python ttk Combobox: https://docs.python.org/3/library/tkinter.ttk.html#combobox
- Python time minute range: https://docs.python.org/3/library/datetime.html#time-objects
- PostgreSQL trigger behavior: https://www.postgresql.org/docs/current/sql-createtrigger.html
- PostgreSQL date/time functions: https://www.postgresql.org/docs/current/functions-datetime.html
