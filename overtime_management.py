"""Tkinter OT window using shared_var.shared_variable and the existing pg_util.

No login, Tk root, connection pool or employee master is created by this module.
See README.md for integration and the trust boundary of a desktop database client.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from importlib import import_module
import logging
import re
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)
BUSINESS_TIMEZONE = "Asia/Kolkata"
OT_EARLIEST = time(15, 0)
OT_LATEST = time(23, 0)
OT_HOURS = tuple(str(hour) for hour in range(3, 12))
OT_MINUTES = ("00", "10", "20", "30", "40", "50")
STATUS_FILTERS = ("All", "Pending", "Approved", "Cancelled", "Rejected")
MAX_OT_MINUTES = 8 * 60  # 3 PM to 11 PM; application policy, not a legal limit.
MAX_REASON_LENGTH = 1000
HISTORY_LIMIT = 200

# Exact attributes of the user's existing SharedVariable instance.
# Do NOT create SharedVariable() here: that would create a second, empty session.
SHARED_FIELD_MAP = {
    "employee_id": "user_emp_id",
    "employee_name": "user_name",
    "department": "department",
}


def resolve_shared_source(source: Any = None) -> Any:
    """Accept shared_var, its shared_variable instance, or an explicit mapping.

    No shared-variable module is imported until the window/reader is used.
    Keeping the module as the window's source also detects singleton replacement
    on logout/login, in addition to edits to the existing singleton's attributes.
    """
    if source is None:
        source = import_module("shared_var")
    if isinstance(source, Mapping):
        source = source.get("shared_variable", source)
    else:
        source = getattr(source, "shared_variable", source)
    if source is None:
        raise ValueError("shared_var.shared_variable is None. Complete login before opening OT.")
    return source


def read_shared_role(source: Any) -> str | None:
    """Snapshot role for session-change detection; this does NOT grant approval."""
    source = resolve_shared_source(source)
    role = source.get("role") if isinstance(source, Mapping) else getattr(source, "role", None)
    if isinstance(role, tk.Variable):
        role = role.get()
    return None if role is None else str(role)


@dataclass(frozen=True)
class Employee:
    employee_id: str
    employee_name: str
    department: str

    @classmethod
    def from_shared(cls, source: Any, field_map: Mapping[str, str] | None = None) -> "Employee":
        """Read plain values or Tkinter variables; call on the Tk/UI thread."""
        source = resolve_shared_source(source)
        mapping = dict(SHARED_FIELD_MAP)
        if field_map:
            unknown = set(field_map) - set(mapping)
            if unknown:
                raise ValueError(f"Unknown field_map key(s): {', '.join(sorted(unknown))}")
            mapping.update(field_map)
        values = {}
        for field, attribute in mapping.items():
            try:
                value = source[attribute] if isinstance(source, Mapping) else getattr(source, attribute)
            except (KeyError, AttributeError) as exc:
                raise ValueError(
                    f"SharedVariable.{attribute} is missing. Set it after login or change field_map."
                ) from exc
            if isinstance(value, tk.Variable):
                value = value.get()
            if value is None or isinstance(value, bool) or not str(value).strip():
                raise ValueError(f"SharedVariable.{attribute} must contain the logged-in employee's {field}. Complete login first.")
            values[field] = str(value).strip()
        return cls(**values)


def validate_ot_time(value: str, label: str = "OT") -> time:
    """Validate the canonical HH:MM value, including ten-minute increments."""
    value = value.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f"Select a valid {label.lower()} hour and minute.")
    hour, minute = map(int, value.split(":"))
    result = time(hour, minute)
    if not OT_EARLIEST <= result <= OT_LATEST:
        raise ValueError(f"{label} time must be between 3:00 PM and 11:00 PM.")
    if minute % 10:
        raise ValueError(f"{label} minutes must be 00, 10, 20, 30, 40 or 50.")
    return result


def time_from_choices(hour: str, minute: str, period: str = "PM") -> str:
    """Convert the three dropdown selections to a canonical 24-hour value."""
    if hour not in OT_HOURS or minute not in OT_MINUTES or period != "PM":
        raise ValueError("Select an hour from 3 to 11, a valid minute, and PM.")
    result = f"{int(hour) + 12:02d}:{minute}"
    validate_ot_time(result)
    return result


def time_text(value: time) -> str:
    """English 12-hour display; supports legacy AM records without changing them."""
    return f"{value.hour % 12 or 12}:{value.minute:02d} {'PM' if value.hour >= 12 else 'AM'}"


def validate_ot(start: str, end: str, reason: str) -> tuple[time, time, int, str]:
    """OT must be same-day, 3:00 PM–11:00 PM, on ten-minute boundaries."""
    first, last = validate_ot_time(start, "Start"), validate_ot_time(end, "End")
    if last <= first:
        raise ValueError("End time must be later than start time on the same day.")
    duration = (last.hour * 60 + last.minute) - (first.hour * 60 + first.minute)
    reason = reason.strip()
    if not reason:
        raise ValueError("Enter the reason for overtime.")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"Reason cannot exceed {MAX_REASON_LENGTH} characters.")
    return first, last, duration, reason


class OTTimePicker(ttk.Frame):
    """Hour / minute / fixed PM Comboboxes. No free-text time entry."""
    def __init__(self, master, on_change: Callable[[], None] | None = None):
        super().__init__(master, style="Overtime.TFrame")
        self.on_change = on_change
        self._changing = False
        self.hour_var = tk.StringVar(self, "")
        self.minute_var = tk.StringVar(self, "00")
        self.period_var = tk.StringVar(self, "PM")
        self.hour_combo = ttk.Combobox(self, textvariable=self.hour_var, values=OT_HOURS,
                                      width=4, state="readonly", height=9)
        self.minute_combo = ttk.Combobox(self, textvariable=self.minute_var, values=OT_MINUTES,
                                        width=4, state="readonly", height=6)
        self.period_combo = ttk.Combobox(self, textvariable=self.period_var, values=("PM",),
                                        width=4, state="readonly", height=1)
        for col, widget in enumerate((self.hour_combo, self.minute_combo, self.period_combo)):
            widget.grid(row=0, column=col, padx=(0, 5))
        self.hour_var.trace_add("write", self._hour_changed)
        self.minute_var.trace_add("write", self._changed)
        self.period_var.trace_add("write", self._changed)

    def _changed(self, *_args):
        if not self._changing and self.on_change is not None:
            self.on_change()

    def _hour_changed(self, *_args):
        values = ("00",) if self.hour_var.get() == "11" else OT_MINUTES
        self.minute_combo.configure(values=values)
        if self.minute_var.get() not in values:
            self.minute_var.set("00")
        self._changed()

    def is_complete(self) -> bool:
        return bool(self.hour_var.get() and self.minute_var.get())

    def get(self) -> str:
        return time_from_choices(self.hour_var.get(), self.minute_var.get(), self.period_var.get())

    def clear(self):
        self._changing = True
        try:
            self.hour_var.set("")
            self.minute_var.set("00")
            self.period_var.set("PM")
        finally:
            self._changing = False
        self._changed()

    def set_time(self, value: time) -> bool:
        """Load only representable values; never silently round legacy records."""
        try:
            if value.second or value.microsecond:
                raise ValueError("Not a minute boundary")
            validate_ot_time(value.strftime("%H:%M"))
        except ValueError:
            self.clear()
            return False
        self._changing = True
        try:
            self.hour_var.set(str(value.hour - 12))
            self.minute_var.set(f"{value.minute:02d}")
            self.period_var.set("PM")
        finally:
            self._changing = False
        self._changed()
        return True


def duration_text(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"{hours} h {remainder:02d} min"


@contextmanager
def db():
    """Borrow one idle connection, commit/rollback, then return it to pg_util.

    Queries execute on the UI thread so an existing single-threaded pg_util pool
    is not silently used from worker threads. A slow connection may delay the UI.
    pg_util.get_connection() must give this operation exclusive use of an idle
    psycopg2-compatible connection, not a connection with another task's work.
    """
    pg_util = import_module("pg_util")
    conn = pg_util.get_connection()
    if conn is None:
        raise RuntimeError("pg_util.get_connection() returned no connection.")
    original_autocommit = getattr(conn, "autocommit", False)
    owns_transaction = False
    try:
        # psycopg2: TRANSACTION_STATUS_IDLE == 0. Do not roll back another task.
        get_status = getattr(conn, "get_transaction_status", None)
        if get_status is not None and get_status() != 0:
            raise RuntimeError("pg_util must return an idle, exclusively borrowed connection.")
        conn.autocommit = False
        owns_transaction = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('TimeZone', %s, true)", (BUSINESS_TIMEZONE,))
            cur.execute("SELECT set_config('statement_timeout', '15000', true), "
                        "set_config('lock_timeout', '5000', true)")
            yield cur
        conn.commit()
    except Exception:
        if owns_transaction:
            try:
                conn.rollback()
            except Exception:
                LOGGER.exception("Rollback failed; pg_util should discard the broken connection.")
        raise
    finally:
        if owns_transaction and not getattr(conn, "closed", False):
            try:
                conn.autocommit = original_autocommit
            except Exception:
                LOGGER.exception("Could not restore connection autocommit.")
        try:
            pg_util.release_connection(conn)
        except Exception:
            # Do not report a successfully committed request as a failed save.
            LOGGER.exception("pg_util.release_connection failed; check the pool implementation.")


@dataclass(frozen=True)
class Request:
    id: int
    employee_id: str
    employee_name: str
    department: str
    ot_date: date
    start_time: time
    end_time: time
    duration_minutes: int
    reason: str
    status: str
    approver_id: str | None
    approver_comment: str | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None


REQUEST_COLUMNS = (
    "id, employee_id, employee_name, department, ot_date, start_time, end_time, "
    "duration_minutes, reason, status, approver_id, approver_comment, "
    "created_at, updated_at, decided_at"
)


class OTService:
    """Application-side rules. Host authentication/authorization must be trusted.

    A boolean from a desktop client is NOT a database security boundary. See the
    deployment notes before giving end users direct database credentials.
    """
    def __init__(self, employee: Employee, is_approver: bool = False,
                 approval_departments: Sequence[str] | None = None, *,
                 allow_self_approval: bool = False):
        if type(allow_self_approval) is not bool:
            raise ValueError("allow_self_approval must be an actual bool.")
        self.allow_self_approval = allow_self_approval
        if type(is_approver) is not bool:
            raise ValueError("is_approver must be an actual bool from your login/permission system.")
        self.employee = employee
        self.is_approver = is_approver
        if isinstance(approval_departments, str):
            raise ValueError("Use a list/tuple for approval_departments, not a single string.")
        departments = (employee.department,) if approval_departments is None else approval_departments
        self.approval_departments = tuple(dict.fromkeys(str(d).strip() for d in departments if str(d).strip()))

    @staticmethod
    def today(cur) -> date:
        # Unlike now(), statement_timestamp() advances between statements in a transaction.
        cur.execute("SELECT (statement_timestamp() AT TIME ZONE %s)::date", (BUSINESS_TIMEZONE,))
        return cur.fetchone()[0]

    def _require_approver(self):
        if not self.is_approver or not self.approval_departments:
            raise PermissionError("You are not authorized to approve overtime.")

    def _audit(self, cur, request_id: int, action: str, details: str = ""):
        cur.execute(
            "INSERT INTO ot_audit(request_id, actor_id, action, details, created_at) "
            "VALUES (%s,%s,%s,%s,clock_timestamp())",
            (request_id, self.employee.employee_id, action, details),
        )

    @staticmethod
    def _validate_status(status: str):
        if status not in STATUS_FILTERS:
            raise ValueError("Invalid status filter.")

    def may_review(self, request: Request) -> bool:
        """UI convenience only; mutation SQL repeats scope/self checks."""
        return (self.is_approver and request.department in self.approval_departments
                and (request.employee_id != self.employee.employee_id or self.allow_self_approval))

    def _require_review_target(self, request: Request):
        self._require_approver()
        if request.department not in self.approval_departments:
            raise PermissionError("This request is outside your authorized departments.")
        if request.employee_id == self.employee.employee_id and not self.allow_self_approval:
            raise PermissionError(
                "Your request is visible, but self-approval and self-cancellation of approved OT "
                "are disabled. Another authorized approver must act on it."
            )

    def mine(self, status: str = "All") -> tuple[date, list[Request]]:
        self._validate_status(status)
        with db() as cur:
            today = self.today(cur)
            cur.execute(
                f"SELECT {REQUEST_COLUMNS} FROM ot_requests WHERE employee_id=%s "
                "AND (%s='All' OR status=%s) "
                "ORDER BY ot_date DESC, id DESC LIMIT %s",
                (self.employee.employee_id, status, status, HISTORY_LIMIT),
            )
            return today, [Request(*row) for row in cur.fetchall()]

    def approval_queue(self, status: str = "Pending") -> tuple[date, list[Request]]:
        self._require_approver()
        self._validate_status(status)
        with db() as cur:
            today = self.today(cur)
            cur.execute(
                f"SELECT {REQUEST_COLUMNS} FROM ot_requests "
                "WHERE ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND (department=ANY(%s) OR employee_id=%s) "
                "AND (%s='All' OR status=%s) ORDER BY created_at, id",
                (BUSINESS_TIMEZONE, list(self.approval_departments),
                 self.employee.employee_id, status, status),
            )
            return today, [Request(*row) for row in cur.fetchall()]

    def submit(self, start: str, end: str, reason: str, expected_day: date) -> int:
        first, last, duration, reason = validate_ot(start, end, reason)
        if not isinstance(expected_day, date):
            raise ValueError("Refresh the screen to obtain today's date from PostgreSQL.")
        employee = self.employee
        with db() as cur:
            # Date comes from this INSERT's server timestamp, not from the PC.
            # The displayed day must still match, so an overnight-open form is not
            # silently submitted for a different date.
            cur.execute(
                "WITH business_day AS (SELECT (statement_timestamp() AT TIME ZONE %s)::date AS day) "
                "INSERT INTO ot_requests(employee_id, employee_name, department, ot_date, "
                "start_time, end_time, duration_minutes, reason) "
                "SELECT %s,%s,%s,day,%s,%s,%s,%s FROM business_day WHERE day=%s RETURNING id",
                (BUSINESS_TIMEZONE, employee.employee_id, employee.employee_name,
                 employee.department, first, last, duration, reason, expected_day),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("The business date changed. Refresh and review your OT before submitting.")
            self._audit(cur, row[0], "Submitted", f"{start.strip()} to {end.strip()}; {duration} minutes")
            return row[0]

    def edit(self, request: Request, start: str, end: str, reason: str):
        first, last, duration, reason = validate_ot(start, end, reason)
        with db() as cur:
            cur.execute(
                "UPDATE ot_requests SET start_time=%s, end_time=%s, duration_minutes=%s, "
                "reason=%s, updated_at=clock_timestamp() "
                "WHERE id=%s AND employee_id=%s AND status='Pending' "
                "AND ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND updated_at=%s RETURNING id",
                (first, last, duration, reason, request.id, self.employee.employee_id,
                 BUSINESS_TIMEZONE, request.updated_at),
            )
            if cur.fetchone() is None:
                raise ValueError("Only your unchanged pending request for today can be edited. Refresh the table.")
            self._audit(cur, request.id, "Edited",
                        f"{request.start_time:%H:%M}-{request.end_time:%H:%M}: {request.reason}\n"
                        f"Changed to {first:%H:%M}-{last:%H:%M}: {reason}")

    def cancel(self, request: Request):
        with db() as cur:
            cur.execute(
                "UPDATE ot_requests SET status='Cancelled', updated_at=clock_timestamp() "
                "WHERE id=%s AND employee_id=%s AND status='Pending' "
                "AND ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND updated_at=%s RETURNING id",
                (request.id, self.employee.employee_id, BUSINESS_TIMEZONE, request.updated_at),
            )
            if cur.fetchone() is None:
                raise ValueError("Only your unchanged pending request for today can be cancelled. Refresh the table.")
            self._audit(cur, request.id, "Cancelled")

    def decide(self, request: Request, approve: bool, comment: str):
        self._require_review_target(request)
        if type(approve) is not bool:
            raise ValueError("approve must be a boolean.")
        comment = comment.strip()
        if not approve and not comment:
            raise ValueError("Enter a comment explaining the rejection.")
        if len(comment) > MAX_REASON_LENGTH:
            raise ValueError(f"Comment cannot exceed {MAX_REASON_LENGTH} characters.")
        if approve:
            # Old requests outside the new window must be edited before approval.
            validate_ot(request.start_time.strftime("%H:%M"),
                        request.end_time.strftime("%H:%M"), request.reason)
            if (request.start_time.second or request.start_time.microsecond
                    or request.end_time.second or request.end_time.microsecond):
                raise ValueError("Request times must be on ten-minute boundaries. Ask the employee to edit it.")
        status = "Approved" if approve else "Rejected"
        with db() as cur:
            cur.execute(
                "UPDATE ot_requests SET status=%s, approver_id=%s, approver_comment=%s, "
                "decided_at=clock_timestamp(), updated_at=clock_timestamp() "
                "WHERE id=%s AND status='Pending' AND (%s OR employee_id<>%s) "
                "AND department=ANY(%s) "
                "AND ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND updated_at=%s RETURNING id",
                (status, self.employee.employee_id, comment, request.id,
                 self.allow_self_approval, self.employee.employee_id, list(self.approval_departments),
                 BUSINESS_TIMEZONE, request.updated_at),
            )
            if cur.fetchone() is None:
                raise ValueError(
                    "This request changed, is not today's pending request, is outside your "
                    "authorized departments, or self-actions are disabled. Refresh and review it again."
                )
            self._audit(cur, request.id, status, comment)

    def cancel_approved(self, request: Request, reason: str):
        """Only an authorized approver may cancel an unchanged approved OT today.

        Original approver/comment/decided_at stay untouched. Actor, time and the
        mandatory cancellation reason are saved in ot_audit in this transaction.
        """
        self._require_review_target(request)
        reason = reason.strip()
        if not reason:
            raise ValueError("Enter a reason for cancelling the approved OT.")
        if len(reason) > MAX_REASON_LENGTH:
            raise ValueError(f"Cancellation reason cannot exceed {MAX_REASON_LENGTH} characters.")
        with db() as cur:
            cur.execute(
                "UPDATE ot_requests SET status='Cancelled', updated_at=clock_timestamp() "
                "WHERE id=%s AND status='Approved' AND department=ANY(%s) "
                "AND (%s OR employee_id<>%s) "
                "AND ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND updated_at=%s RETURNING id",
                (request.id, list(self.approval_departments), self.allow_self_approval,
                 self.employee.employee_id, BUSINESS_TIMEZONE, request.updated_at),
            )
            if cur.fetchone() is None:
                raise ValueError(
                    "Only an unchanged approved request for today within your permission scope "
                    "can be cancelled. Refresh and review it again."
                )
            self._audit(cur, request.id, "CancelledApproved", reason)

    def audit_history(self, request_id: int) -> list[tuple]:
        with db() as cur:
            cur.execute(
                "SELECT a.created_at, a.actor_id, a.action, a.details FROM ot_audit a "
                "JOIN ot_requests r ON r.id=a.request_id WHERE r.id=%s "
                "AND (r.employee_id=%s OR (%s AND r.department=ANY(%s))) ORDER BY a.id",
                (request_id, self.employee.employee_id, self.is_approver,
                 list(self.approval_departments)),
            )
            return cur.fetchall()


class OvertimeWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, *, shared_vars: Any = None,
                 field_map: Mapping[str, str] | None = None,
                 is_approver: bool = False,
                 approval_departments: Sequence[str] | None = None,
                 allow_self_approval: bool = False):
        source = import_module("shared_var") if shared_vars is None else shared_vars
        employee = Employee.from_shared(source, field_map)
        service = OTService(employee, is_approver, approval_departments,
                            allow_self_approval=allow_self_approval)
        session_role = read_shared_role(source)
        super().__init__(master)
        self.shared_vars = source
        self.field_map = dict(field_map or {})
        self.employee = employee
        self.session_role = session_role
        self.service = service
        self.server_day: date | None = None
        self.records: dict[int, Request] = {}
        self.view = "apply"
        self.day_label = tk.StringVar(self, "Business date: loading from database...")
        self.status_text = tk.StringVar(self, "")
        self.apply_filter_var = tk.StringVar(self, "All")
        self.filter_var = tk.StringVar(self, "Pending")
        self.title("Overtime Management")
        self.geometry("1180x760")
        self.minsize(1100, 720)
        self.configure(bg="white")
        # Namespaced styles leave the host application's existing ttk styles alone.
        style = ttk.Style(self)
        style.configure("Overtime.TFrame", background="white")
        style.configure("Overtime.TLabel", background="white", foreground="#202020")
        style.configure("Overtime.TLabelframe", background="white")
        style.configure("Overtime.TLabelframe.Label", background="white", foreground="#202020")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build_shell()
        self.show_apply()

    def _build_shell(self):
        sidebar = tk.Frame(self, bg="white", width=210, highlightbackground="black", highlightthickness=2)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        for title, command in (("Apply", self.show_apply), ("Approve", self.show_approve)):
            button = tk.Button(sidebar, text=title, command=command, bg="#00A5DD", fg="white",
                               activebackground="#0787BA", activeforeground="white",
                               disabledforeground="#EEEEEE", font=("Segoe UI", 14), relief="solid", bd=2)
            button.pack(fill="x", padx=10, pady=(10, 0), ipady=5)
            if title == "Approve" and not self.service.is_approver:
                button.configure(state="disabled")
        tk.Label(sidebar, text="Logged-in employee", bg="white", fg="#555555",
                 font=("Segoe UI", 10)).pack(anchor="w", padx=14, pady=(24, 5))
        tk.Label(sidebar, text=f"{self.employee.employee_name}\n{self.employee.employee_id}\n{self.employee.department}",
                 bg="white", justify="left", wraplength=176, font=("Segoe UI", 11)).pack(anchor="w", padx=14)
        tk.Button(sidebar, text="Close", command=self.destroy).pack(side="bottom", fill="x", padx=12, pady=12)
        tk.Label(sidebar, text="Apply and approve\ntoday's OT only.\n\nHistory is read-only.",
                 bg="white", fg="#555555", justify="left", font=("Segoe UI", 10)).pack(side="bottom", padx=14, pady=10)
        self.content = ttk.Frame(self, padding=18, style="Overtime.TFrame")
        self.content.pack(side="left", fill="both", expand=True)

    def validate_session(self) -> None:
        """Reject an old window after identity/role changes in the host application.

        This is a UI/session safeguard, not server-side authorization. The host
        should close every OT window on logout or permission revocation.
        """
        if Employee.from_shared(self.shared_vars, self.field_map) != self.employee:
            raise PermissionError("The logged-in employee changed. Close and reopen this OT window.")
        if read_shared_role(self.shared_vars) != self.session_role:
            raise PermissionError("Your role changed. Close and reopen this OT window.")

    def _guard(self, action: Callable[[], Any]):
        try:
            self.validate_session()
            return action()
        except (ValueError, PermissionError) as exc:
            messagebox.showwarning("Overtime Management", str(exc), parent=self)
        except Exception as exc:
            code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
            if code == "23505":
                text = ("An OT request already exists for you today. Edit the existing pending request. "
                        "This version permits one request per day, including cancelled/rejected requests.")
            elif code == "42P01":
                text = "The OT tables are missing. Run schema.sql once in your application's database."
            elif code == "23514":
                text = ("The database rejected the OT values. Use 3:00 PM–11:00 PM in "
                        "ten-minute steps, an end later than the start, and a nonblank reason.")
            elif code in ("57014", "55P03"):
                text = "The database operation timed out or was busy. Refresh the list before retrying."
            else:
                text = "The operation could not be confirmed. Refresh before retrying; check the application log and pg_util connection."
            LOGGER.exception("OT operation failed")
            messagebox.showerror("Overtime Management", text, parent=self)

    def _clear(self, heading: str):
        for widget in self.content.winfo_children():
            widget.destroy()
        self.records.clear()
        self.status_text.set("")
        ttk.Label(self.content, style="Overtime.TLabel", text=heading, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(self.content, style="Overtime.TLabel", textvariable=self.day_label).pack(anchor="w", pady=(3, 12))

    def _set_day(self, day: date):
        self.server_day = day
        self.day_label.set(f"Business date: {day:%d %b %Y}  |  {BUSINESS_TIMEZONE}  |  Database time")

    def _identity_fields(self):
        frame = ttk.LabelFrame(self.content, style="Overtime.TLabelframe", text="Employee details — logged-in user", padding=10)
        frame.pack(fill="x", pady=(0, 12))
        self.identity_vars = []
        for col, (label, value) in enumerate((("Employee ID", self.employee.employee_id),
                                            ("Name", self.employee.employee_name),
                                            ("Department", self.employee.department))):
            frame.columnconfigure(col, weight=1)
            ttk.Label(frame, style="Overtime.TLabel", text=label).grid(row=0, column=col, sticky="w", padx=5)
            variable = tk.StringVar(self, value)
            self.identity_vars.append(variable)
            ttk.Entry(frame, textvariable=variable, state="readonly", width=22).grid(
                row=1, column=col, sticky="ew", padx=5, pady=(4, 0))

    def _make_table(self, columns: Sequence[tuple[str, str, int]]) -> ttk.Treeview:
        frame = ttk.Frame(self.content, style="Overtime.TFrame")
        frame.pack(fill="both", expand=True, pady=(8, 4))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse", height=7)
        for column, title, width in columns:
            tree.heading(column, text=title)
            tree.column(column, width=width, minwidth=60, stretch=column in ("reason", "name"))
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        ttk.Label(self.content, style="Overtime.TLabel", textvariable=self.status_text).pack(anchor="w")
        tree.bind("<Double-1>", lambda _event: self._guard(self.view_details))
        return tree

    def show_apply(self):
        self.view = "apply"
        self._clear("Apply for today's overtime")
        self._identity_fields()
        form = ttk.LabelFrame(self.content, style="Overtime.TLabelframe", text="Enter OT details", padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        self.duration_var = tk.StringVar(self, "Duration: —")
        ttk.Label(form, style="Overtime.TLabel", text="Start time").grid(row=0, column=0, sticky="w", padx=4)
        self.start_picker = OTTimePicker(form)
        self.start_picker.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(form, style="Overtime.TLabel", text="End time").grid(row=0, column=2, sticky="w", padx=4)
        self.end_picker = OTTimePicker(form)
        self.end_picker.grid(row=0, column=3, sticky="w", padx=6)
        self.start_picker.on_change = self._preview_duration
        self.end_picker.on_change = self._preview_duration
        ttk.Label(form, style="Overtime.TLabel", textvariable=self.duration_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(7, 3))
        ttk.Label(form, style="Overtime.TLabel",
                  text="Hour / minute / PM  •  3:00 PM–11:00 PM  •  10-minute steps  •  Same day only").grid(
            row=2, column=0, columnspan=4, sticky="w")
        ttk.Label(form, style="Overtime.TLabel", text="Reason (required)").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(8, 3))
        self.reason_box = ScrolledText(form, height=3, wrap="word", font=("Segoe UI", 10))
        self.reason_box.grid(row=4, column=0, columnspan=4, sticky="ew")
        actions = ttk.Frame(self.content, style="Overtime.TFrame")
        actions.pack(fill="x", pady=10)
        self.apply_buttons = {}
        buttons = (("Submit OT", self.submit), ("Save changes", self.edit_selected),
                   ("Cancel pending", self.cancel_selected), ("Clear form", self.clear_form),
                   ("View details", self.view_details), ("Refresh", self.refresh_mine))
        for title, action in buttons:
            button = ttk.Button(actions, text=title, command=lambda fn=action: self._guard(fn))
            button.pack(side="left", padx=(0, 6))
            self.apply_buttons[title] = button
        bar = ttk.Frame(self.content, style="Overtime.TFrame")
        bar.pack(fill="x", pady=(2, 0))
        ttk.Label(bar, style="Overtime.TLabel", text="My requests", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(bar, style="Overtime.TLabel", text="Show status:").pack(side="left", padx=(18, 4))
        self.apply_filter_combo = ttk.Combobox(bar, textvariable=self.apply_filter_var, state="readonly",
                                              width=13, values=STATUS_FILTERS)
        self.apply_filter_combo.pack(side="left")
        self.apply_filter_combo.bind("<<ComboboxSelected>>", lambda _event: self._guard(self.refresh_mine))
        ttk.Label(bar, style="Overtime.TLabel", text=f"Latest {HISTORY_LIMIT} matching requests").pack(side="left", padx=10)
        self.tree = self._make_table((("id", "ID", 55), ("date", "OT date", 100),
                                      ("start", "Start", 85), ("end", "End", 100),
                                      ("duration", "Duration", 100), ("status", "Status", 90),
                                      ("reason", "Reason", 210)))
        self.tree.bind("<<TreeviewSelect>>", self._load_selected)
        self._update_apply_actions()
        self._guard(self.refresh_mine)

    def _preview_duration(self, *_args):
        if not self.start_picker.is_complete() or not self.end_picker.is_complete():
            self.duration_var.set("Duration: —")
            return
        try:
            _first, _last, minutes, _reason = validate_ot(
                self.start_picker.get(), self.end_picker.get(), "preview")
            self.duration_var.set(f"Duration: {duration_text(minutes)}")
        except ValueError as exc:
            self.duration_var.set(str(exc))

    def _selected(self) -> Request:
        selection = self.tree.selection()
        if not selection or int(selection[0]) not in self.records:
            raise ValueError("Select a request from the table first.")
        return self.records[int(selection[0])]

    def _update_apply_actions(self):
        selection = self.tree.selection()
        request = self.records.get(int(selection[0])) if selection else None
        editable = bool(request and request.status == "Pending" and request.ot_date == self.server_day
                        and request.employee_id == self.employee.employee_id)
        for key in ("Save changes", "Cancel pending"):
            self.apply_buttons[key].configure(state="normal" if editable else "disabled")
        self.apply_buttons["View details"].configure(state="normal" if request else "disabled")

    def _load_selected(self, _event=None):
        self._update_apply_actions()
        if not self.tree.selection():
            return
        request = self._selected()
        start_ok = self.start_picker.set_time(request.start_time)
        end_ok = self.end_picker.set_time(request.end_time)
        self.reason_box.delete("1.0", "end")
        self.reason_box.insert("1.0", request.reason)
        suffix = "Editable today." if request.status == "Pending" and request.ot_date == self.server_day else "Read-only request."
        if not (start_ok and end_ok):
            suffix += " Legacy time outside current choices; see table/details. It has not been rounded."
        self.status_text.set(f"Selected #{request.id} — {request.status}. {suffix}")

    def clear_form(self):
        self.tree.selection_remove(*self.tree.selection())
        self.start_picker.clear()
        self.end_picker.clear()
        self.reason_box.delete("1.0", "end")
        self._update_apply_actions()
        self.status_text.set("Choose your OT start time, end time and reason.")

    def refresh_mine(self):
        status = self.apply_filter_var.get()
        day, requests = self.service.mine(status)
        self._set_day(day)
        self.tree.delete(*self.tree.get_children())
        self.records = {request.id: request for request in requests}
        for request in requests:
            end = time_text(request.end_time) + (" (+1d)" if request.end_time < request.start_time else "")
            self.tree.insert("", "end", iid=str(request.id), values=(request.id, request.ot_date,
                             time_text(request.start_time), end, duration_text(request.duration_minutes),
                             request.status, request.reason.replace("\n", " ")))
        self._update_apply_actions()
        self.status_text.set(f"{len(requests)} request(s) — {status}. Only today's pending OT is editable here.")

    def _refresh_after_save(self, refresh: Callable[[], None]):
        try:
            refresh()
        except Exception:
            LOGGER.exception("OT was saved but list refresh failed")
            messagebox.showwarning("Saved; refresh failed", "The change was saved. Refresh the table to see it; "
                                   "do not submit it again.", parent=self)

    def submit(self):
        start, end = self.start_picker.get(), self.end_picker.get()
        reason = self.reason_box.get("1.0", "end-1c")
        validate_ot(start, end, reason)
        request_id = self.service.submit(start, end, reason, self.server_day)
        self.clear_form()
        messagebox.showinfo("Submitted", f"OT request #{request_id} submitted for approval.", parent=self)
        self._refresh_after_save(self.refresh_mine)

    def edit_selected(self):
        request = self._selected()
        self._check_pending_today(request)
        start, end = self.start_picker.get(), self.end_picker.get()
        reason = self.reason_box.get("1.0", "end-1c")
        validate_ot(start, end, reason)
        self.service.edit(request, start, end, reason)
        messagebox.showinfo("Updated", f"OT request #{request.id} updated.", parent=self)
        self._refresh_after_save(self.refresh_mine)

    def _check_pending_today(self, request: Request):
        if request.status != "Pending" or request.ot_date != self.server_day:
            raise ValueError("Only today's pending requests can be changed.")

    def cancel_selected(self):
        request = self._selected()
        self._check_pending_today(request)
        if messagebox.askyesno("Cancel OT", f"Cancel request #{request.id}?\n\nThe one-request-per-day rule "
                               "means you cannot submit another request today. Use Save changes to change times/reason.", parent=self):
            self.service.cancel(request)
            self.clear_form()
            self._refresh_after_save(self.refresh_mine)

    def show_approve(self):
        def show():
            self.service._require_approver()
            self.view = "approve"
            self._clear("Approve today's overtime")
            ttk.Label(self.content, style="Overtime.TLabel", text="Authorized departments: " + ", ".join(self.service.approval_departments),
                      wraplength=750).pack(anchor="w", pady=(0, 10))
            bar = ttk.Frame(self.content, style="Overtime.TFrame")
            bar.pack(fill="x")
            ttk.Label(bar, style="Overtime.TLabel", text="Show status:").pack(side="left")
            combo = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly", width=14,
                                 values=STATUS_FILTERS)
            combo.pack(side="left", padx=8)
            combo.bind("<<ComboboxSelected>>", lambda _event: self._guard(self.refresh_pending))
            ttk.Button(bar, text="Refresh", command=lambda: self._guard(self.refresh_pending)).pack(side="left")
            ttk.Label(self.content, style="Overtime.TLabel",
                      text="Comment / cancellation reason (required for rejection or approved-OT cancellation)").pack(anchor="w", pady=(14, 4))
            self.comment_box = ScrolledText(self.content, height=3, wrap="word", font=("Segoe UI", 10))
            self.comment_box.pack(fill="x")
            actions = ttk.Frame(self.content, style="Overtime.TFrame")
            actions.pack(fill="x", pady=10)
            self.approval_buttons = {}
            for title, fn in (("Approve selected", lambda: self.decide(True)),
                              ("Reject selected", lambda: self.decide(False)),
                              ("Cancel approved OT", self.cancel_approved_selected),
                              ("View details", self.view_details)):
                button = ttk.Button(actions, text=title, command=lambda action=fn: self._guard(action))
                button.pack(side="left", padx=(0, 8))
                self.approval_buttons[title] = button
            own_policy = ("Self-actions are enabled within your authorized departments."
                          if self.service.allow_self_approval else "Your own OT is view-only; another approver must act.")
            ttk.Label(self.content, style="Overtime.TLabel", wraplength=850,
                      text="Your requests are included and marked (you). " + own_policy).pack(anchor="w")
            self.tree = self._make_table((("id", "ID", 50), ("employee_id", "Employee ID", 95),
                                          ("name", "Name", 150), ("department", "Department", 110),
                                          ("start", "Start", 85), ("end", "End", 100),
                                          ("duration", "Duration", 100), ("status", "Status", 90),
                                          ("reason", "Reason", 220)))
            self.tree.tag_configure("own", background="#FFF2D5")
            self.tree.bind("<<TreeviewSelect>>", self._approval_selection_changed)
            self._update_approval_actions()
            self.refresh_pending()
        self._guard(show)

    def _approval_selection_changed(self, _event=None):
        self.comment_box.delete("1.0", "end")
        self._update_approval_actions()
        if self.tree.selection():
            request = self._selected()
            suffix = "Select an available action."
            if request.employee_id == self.employee.employee_id and not self.service.allow_self_approval:
                suffix = "Your own OT: view only. Another approver must act."
            elif request.department not in self.service.approval_departments:
                suffix = "View only: outside your authorized departments."
            elif request.status in ("Cancelled", "Rejected"):
                suffix = "Finalized: read only."
            self.status_text.set(f"Selected #{request.id} — {request.status}. {suffix}")

    def _update_approval_actions(self):
        selection = self.tree.selection()
        request = self.records.get(int(selection[0])) if selection else None
        reviewable = bool(request and request.ot_date == self.server_day and self.service.may_review(request))
        pending = reviewable and request.status == "Pending"
        approved = reviewable and request.status == "Approved"
        for key in ("Approve selected", "Reject selected"):
            self.approval_buttons[key].configure(state="normal" if pending else "disabled")
        self.approval_buttons["Cancel approved OT"].configure(state="normal" if approved else "disabled")
        self.approval_buttons["View details"].configure(state="normal" if request else "disabled")

    def refresh_pending(self):
        day, requests = self.service.approval_queue(self.filter_var.get())
        self._set_day(day)
        self.tree.delete(*self.tree.get_children())
        self.records = {request.id: request for request in requests}
        self.comment_box.delete("1.0", "end")
        for request in requests:
            end = time_text(request.end_time) + (" (+1d)" if request.end_time < request.start_time else "")
            own = request.employee_id == self.employee.employee_id
            self.tree.insert("", "end", iid=str(request.id), tags=("own",) if own else (),
                             values=(request.id, request.employee_id, request.employee_name + (" (you)" if own else ""),
                                     request.department, time_text(request.start_time), end,
                                     duration_text(request.duration_minutes), request.status, request.reason.replace("\n", " ")))
        self._update_approval_actions()
        self.status_text.set(f"{len(requests)} request(s) for today — {self.filter_var.get()}.")

    def decide(self, approve: bool):
        request = self._selected()
        self._check_pending_today(request)
        self.service._require_review_target(request)
        comment = self.comment_box.get("1.0", "end-1c").strip()
        if not approve and not comment:
            raise ValueError("Enter a comment explaining the rejection.")
        action = "Approve" if approve else "Reject"
        if not messagebox.askyesno(action + " OT", f"{action} request #{request.id} for {request.employee_name}?\n"
                                   f"{time_text(request.start_time)}–{time_text(request.end_time)} | "
                                   f"{duration_text(request.duration_minutes)}", parent=self):
            return
        self.service.decide(request, approve, comment)
        messagebox.showinfo("Decision saved", f"Request #{request.id} {'approved' if approve else 'rejected'}.", parent=self)
        self._refresh_after_save(self.refresh_pending)

    def cancel_approved_selected(self):
        request = self._selected()
        self.service._require_review_target(request)
        if request.status != "Approved" or request.ot_date != self.server_day:
            raise ValueError("Select today's approved OT to cancel.")
        reason = self.comment_box.get("1.0", "end-1c").strip()
        if not reason:
            raise ValueError("Enter a reason for cancelling the approved OT.")
        if len(reason) > MAX_REASON_LENGTH:
            raise ValueError(f"Cancellation reason cannot exceed {MAX_REASON_LENGTH} characters.")
        if not messagebox.askyesno(
            "Cancel approved OT", f"Cancel approved request #{request.id} for {request.employee_name}?\n\n"
            f"Reason: {reason}\n\nThe approval history will be retained. "
            "The employee cannot submit another request for this date.", parent=self
        ):
            return
        self.service.cancel_approved(request, reason)
        messagebox.showinfo("OT cancelled", f"Approved OT #{request.id} was cancelled and logged.", parent=self)
        self._refresh_after_save(self.refresh_pending)

    def view_details(self):
        request = self._selected()
        audit = self.service.audit_history(request.id)
        dialog = tk.Toplevel(self)
        dialog.title(f"OT request #{request.id}")
        dialog.geometry("740x560")
        dialog.transient(self)
        text = ScrolledText(dialog, wrap="word", font=("Segoe UI", 10), padx=12, pady=12)
        text.pack(fill="both", expand=True)
        next_day = " (next day)" if request.end_time < request.start_time else ""
        content = (
            f"Request #{request.id} | {request.status}\n"
            f"Employee: {request.employee_id} — {request.employee_name}\n"
            f"Department: {request.department}\nOT date: {request.ot_date}\n"
            f"Time: {time_text(request.start_time)} to {time_text(request.end_time)}{next_day}\n"
            f"Duration: {duration_text(request.duration_minutes)}\n\nREASON\n{request.reason}\n\n"
            f"Approver: {request.approver_id or '—'}\nComment: {request.approver_comment or '—'}\n"
            f"Decision time: {request.decided_at or '—'}\n\nAUDIT HISTORY\n"
        )
        for timestamp, actor, action, details in audit:
            if action == "CancelledApproved":
                action = "Approved OT cancelled by approver"
            content += f"\n{timestamp} | {actor} | {action}\n{details or ''}\n"
        text.insert("1.0", content)
        text.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=8)


if __name__ == "__main__":
    raise SystemExit("Import OvertimeWindow into your existing Tkinter application; see example_integration.py.")
