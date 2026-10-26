"""Tkinter OT window using the host application's shared_var and pg_util.

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
MAX_OT_MINUTES = 16 * 60  # Application policy, not a statutory/legal limit.
MAX_REASON_LENGTH = 1000
HISTORY_LIMIT = 200

# Only shared_var.name was specified. Adjust the other attribute names here,
# or pass field_map=... when creating the window.
SHARED_FIELD_MAP = {
    "employee_id": "employee_id",
    "employee_name": "name",
    "department": "department",
}


@dataclass(frozen=True)
class Employee:
    employee_id: str
    employee_name: str
    department: str

    @classmethod
    def from_shared(cls, source: Any, field_map: Mapping[str, str] | None = None) -> "Employee":
        """Read plain values or Tkinter variables; call on the Tk/UI thread."""
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
                    f"shared_var.{attribute} is missing. Set it after login or change field_map."
                ) from exc
            if isinstance(value, tk.Variable):
                value = value.get()
            if value is None or isinstance(value, bool) or not str(value).strip():
                raise ValueError(f"shared_var.{attribute} must contain the logged-in employee's {field}.")
            values[field] = str(value).strip()
        return cls(**values)


def validate_ot(start: str, end: str, reason: str) -> tuple[time, time, int, str]:
    """Validate times to minute precision. An earlier end means the next day."""
    times = []
    for label, value in (("Start", start), ("End", end)):
        value = value.strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{label} time must be in 24-hour HH:MM format, for example 18:30.")
        hour, minute = map(int, value.split(":"))
        times.append(time(hour, minute))
    first, last = times
    duration = ((last.hour * 60 + last.minute) - (first.hour * 60 + first.minute)) % 1440
    if duration == 0:
        raise ValueError("Start and end times cannot be the same.")
    if duration > MAX_OT_MINUTES:
        raise ValueError(f"OT cannot exceed {MAX_OT_MINUTES // 60} hours per request.")
    reason = reason.strip()
    if not reason:
        raise ValueError("Enter the reason for overtime.")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"Reason cannot exceed {MAX_REASON_LENGTH} characters.")
    return first, last, duration, reason


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
                 approval_departments: Sequence[str] | None = None):
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
            "INSERT INTO ot_audit(request_id, actor_id, action, details) VALUES (%s,%s,%s,%s)",
            (request_id, self.employee.employee_id, action, details),
        )

    def mine(self) -> tuple[date, list[Request]]:
        with db() as cur:
            today = self.today(cur)
            cur.execute(
                f"SELECT {REQUEST_COLUMNS} FROM ot_requests WHERE employee_id=%s "
                "ORDER BY ot_date DESC, id DESC LIMIT %s",
                (self.employee.employee_id, HISTORY_LIMIT),
            )
            return today, [Request(*row) for row in cur.fetchall()]

    def approval_queue(self, status: str = "Pending") -> tuple[date, list[Request]]:
        self._require_approver()
        if status not in ("Pending", "Approved", "Rejected", "Cancelled", "All"):
            raise ValueError("Invalid status filter.")
        with db() as cur:
            today = self.today(cur)
            cur.execute(
                f"SELECT {REQUEST_COLUMNS} FROM ot_requests "
                "WHERE ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND employee_id<>%s AND department=ANY(%s) "
                "AND (%s='All' OR status=%s) ORDER BY created_at, id",
                (BUSINESS_TIMEZONE, self.employee.employee_id,
                 list(self.approval_departments), status, status),
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
        self._require_approver()
        if type(approve) is not bool:
            raise ValueError("approve must be a boolean.")
        comment = comment.strip()
        if not approve and not comment:
            raise ValueError("Enter a comment explaining the rejection.")
        if len(comment) > MAX_REASON_LENGTH:
            raise ValueError(f"Comment cannot exceed {MAX_REASON_LENGTH} characters.")
        status = "Approved" if approve else "Rejected"
        with db() as cur:
            cur.execute(
                "UPDATE ot_requests SET status=%s, approver_id=%s, approver_comment=%s, "
                "decided_at=clock_timestamp(), updated_at=clock_timestamp() "
                "WHERE id=%s AND status='Pending' AND employee_id<>%s "
                "AND department=ANY(%s) "
                "AND ot_date=(statement_timestamp() AT TIME ZONE %s)::date "
                "AND updated_at=%s RETURNING id",
                (status, self.employee.employee_id, comment, request.id,
                 self.employee.employee_id, list(self.approval_departments),
                 BUSINESS_TIMEZONE, request.updated_at),
            )
            if cur.fetchone() is None:
                raise ValueError(
                    "This request changed, is not today's pending request, is outside your "
                    "authorized departments, or belongs to you. Refresh and review it again."
                )
            self._audit(cur, request.id, status, comment)

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
                 approval_departments: Sequence[str] | None = None):
        source = import_module("shared_var") if shared_vars is None else shared_vars
        employee = Employee.from_shared(source, field_map)
        service = OTService(employee, is_approver, approval_departments)
        super().__init__(master)
        self.shared_vars = source
        self.field_map = dict(field_map or {})
        self.employee = employee
        self.service = service
        self.server_day: date | None = None
        self.records: dict[int, Request] = {}
        self.view = "apply"
        self.day_label = tk.StringVar(self, "Business date: loading from database...")
        self.status_text = tk.StringVar(self, "")
        self.title("Overtime Management")
        self.geometry("1180x760")
        self.minsize(1040, 680)
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

    def _guard(self, action: Callable[[], Any]):
        try:
            # Changing users in the main app must not leave an old identity active here.
            if Employee.from_shared(self.shared_vars, self.field_map) != self.employee:
                raise PermissionError("The logged-in employee changed. Close and reopen this OT window.")
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
        frame = ttk.LabelFrame(self.content, style="Overtime.TLabelframe", text="Employee details — from shared_var", padding=10)
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
        self.start_var = tk.StringVar(self)
        self.end_var = tk.StringVar(self)
        self.duration_var = tk.StringVar(self, "Duration: —")
        ttk.Label(form, style="Overtime.TLabel", text="Start (HH:MM)").grid(row=0, column=0, sticky="w", padx=4)
        ttk.Entry(form, textvariable=self.start_var, width=10).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(form, style="Overtime.TLabel", text="End (HH:MM)").grid(row=0, column=2, sticky="w", padx=4)
        ttk.Entry(form, textvariable=self.end_var, width=10).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(form, style="Overtime.TLabel", textvariable=self.duration_var).grid(row=1, column=0, columnspan=4, sticky="w", pady=(7, 3))
        ttk.Label(form, style="Overtime.TLabel", text="24-hour times. An end earlier than the start means the next day.").grid(
            row=2, column=0, columnspan=4, sticky="w")
        ttk.Label(form, style="Overtime.TLabel", text="Reason (required)").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 3))
        self.reason_box = ScrolledText(form, height=3, wrap="word", font=("Segoe UI", 10))
        self.reason_box.grid(row=4, column=0, columnspan=4, sticky="ew")
        for variable in (self.start_var, self.end_var):
            variable.trace_add("write", self._preview_duration)
        actions = ttk.Frame(self.content, style="Overtime.TFrame")
        actions.pack(fill="x", pady=10)
        buttons = (("Submit OT", self.submit), ("Save changes", self.edit_selected),
                   ("Cancel selected", self.cancel_selected), ("Clear form", self.clear_form),
                   ("View details", self.view_details), ("Refresh", self.refresh_mine))
        for title, action in buttons:
            ttk.Button(actions, text=title, command=lambda fn=action: self._guard(fn)).pack(side="left", padx=(0, 6))
        ttk.Label(self.content, style="Overtime.TLabel", text=f"My requests — latest {HISTORY_LIMIT}; select a pending request to edit.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tree = self._make_table((("id", "ID", 55), ("date", "OT date", 100),
                                      ("start", "Start", 70), ("end", "End", 90),
                                      ("duration", "Duration", 100), ("status", "Status", 90),
                                      ("reason", "Reason", 210)))
        self.tree.bind("<<TreeviewSelect>>", self._load_selected)
        self._guard(self.refresh_mine)

    def _preview_duration(self, *_args):
        if not self.start_var.get().strip() or not self.end_var.get().strip():
            self.duration_var.set("Duration: —")
            return
        try:
            first, last, minutes, _reason = validate_ot(self.start_var.get(), self.end_var.get(), "preview")
            suffix = "  •  Ends next day" if last < first else ""
            self.duration_var.set(f"Duration: {duration_text(minutes)}{suffix}")
        except ValueError as exc:
            self.duration_var.set(str(exc))

    def _selected(self) -> Request:
        selection = self.tree.selection()
        if not selection:
            raise ValueError("Select a request from the table first.")
        return self.records[int(selection[0])]

    def _load_selected(self, _event=None):
        if not self.tree.selection():
            return
        request = self._selected()
        self.start_var.set(request.start_time.strftime("%H:%M"))
        self.end_var.set(request.end_time.strftime("%H:%M"))
        self.reason_box.delete("1.0", "end")
        self.reason_box.insert("1.0", request.reason)
        suffix = "Editable today." if request.status == "Pending" and request.ot_date == self.server_day else "Read-only request."
        self.status_text.set(f"Selected #{request.id} — {request.status}. {suffix}")

    def clear_form(self):
        self.tree.selection_remove(*self.tree.selection())
        self.start_var.set("")
        self.end_var.set("")
        self.reason_box.delete("1.0", "end")
        self.status_text.set("Enter your OT start time, end time and reason.")

    def refresh_mine(self):
        day, requests = self.service.mine()
        self._set_day(day)
        self.tree.delete(*self.tree.get_children())
        self.records = {request.id: request for request in requests}
        for request in requests:
            end = request.end_time.strftime("%H:%M") + (" (+1d)" if request.end_time < request.start_time else "")
            self.tree.insert("", "end", iid=str(request.id), values=(request.id, request.ot_date,
                             request.start_time.strftime("%H:%M"), end, duration_text(request.duration_minutes),
                             request.status, request.reason.replace("\n", " ")))
        today_requests = [request for request in requests if request.ot_date == day]
        text = f"{len(requests)} request(s) loaded. "
        text += f"Today's request: {today_requests[0].status}." if today_requests else "No request submitted today."
        self.status_text.set(text)

    def _confirm_overnight(self, first: time, last: time) -> bool:
        return last >= first or messagebox.askyesno(
            "Overnight OT", f"OT starts at {first:%H:%M} on today's business date and ends "
            f"at {last:%H:%M} the next day. Continue?", parent=self)

    def _refresh_after_save(self, refresh: Callable[[], None]):
        try:
            refresh()
        except Exception:
            LOGGER.exception("OT was saved but list refresh failed")
            messagebox.showwarning("Saved; refresh failed", "The change was saved. Refresh the table to see it; "
                                   "do not submit it again.", parent=self)

    def submit(self):
        start, end, reason = self.start_var.get(), self.end_var.get(), self.reason_box.get("1.0", "end-1c")
        first, last, _minutes, _reason = validate_ot(start, end, reason)
        if not self._confirm_overnight(first, last):
            return
        request_id = self.service.submit(start, end, reason, self.server_day)
        self.clear_form()
        messagebox.showinfo("Submitted", f"OT request #{request_id} submitted for approval.", parent=self)
        self._refresh_after_save(self.refresh_mine)

    def edit_selected(self):
        request = self._selected()
        self._check_pending_today(request)
        start, end, reason = self.start_var.get(), self.end_var.get(), self.reason_box.get("1.0", "end-1c")
        first, last, _minutes, _reason = validate_ot(start, end, reason)
        if not self._confirm_overnight(first, last):
            return
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
            self.filter_var = tk.StringVar(self, "Pending")
            combo = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly", width=14,
                                 values=("Pending", "Approved", "Rejected", "Cancelled", "All"))
            combo.pack(side="left", padx=8)
            combo.bind("<<ComboboxSelected>>", lambda _event: self._guard(self.refresh_pending))
            ttk.Button(bar, text="Refresh", command=lambda: self._guard(self.refresh_pending)).pack(side="left")
            ttk.Label(self.content, style="Overtime.TLabel", text="Approver comment (required for rejection)").pack(anchor="w", pady=(14, 4))
            self.comment_box = ScrolledText(self.content, height=3, wrap="word", font=("Segoe UI", 10))
            self.comment_box.pack(fill="x")
            actions = ttk.Frame(self.content, style="Overtime.TFrame")
            actions.pack(fill="x", pady=10)
            for title, fn in (("Approve selected", lambda: self.decide(True)),
                              ("Reject selected", lambda: self.decide(False)), ("View details", self.view_details)):
                ttk.Button(actions, text=title, command=lambda action=fn: self._guard(action)).pack(side="left", padx=(0, 8))
            ttk.Label(self.content, style="Overtime.TLabel", text="Your own requests are excluded. Review the full reason in View details.").pack(anchor="w")
            self.tree = self._make_table((("id", "ID", 50), ("employee_id", "Employee ID", 95),
                                          ("name", "Name", 140), ("department", "Department", 110),
                                          ("start", "Start", 65), ("end", "End", 90),
                                          ("duration", "Duration", 100), ("status", "Status", 90),
                                          ("reason", "Reason", 220)))
            self.tree.bind("<<TreeviewSelect>>", lambda _event: self.comment_box.delete("1.0", "end"))
            self.refresh_pending()
        self._guard(show)

    def refresh_pending(self):
        day, requests = self.service.approval_queue(self.filter_var.get())
        self._set_day(day)
        self.tree.delete(*self.tree.get_children())
        self.records = {request.id: request for request in requests}
        self.comment_box.delete("1.0", "end")
        for request in requests:
            end = request.end_time.strftime("%H:%M") + (" (+1d)" if request.end_time < request.start_time else "")
            self.tree.insert("", "end", iid=str(request.id), values=(request.id, request.employee_id,
                             request.employee_name, request.department, request.start_time.strftime("%H:%M"), end,
                             duration_text(request.duration_minutes), request.status, request.reason.replace("\n", " ")))
        self.status_text.set(f"{len(requests)} request(s) for today — {self.filter_var.get()}.")

    def decide(self, approve: bool):
        request = self._selected()
        self._check_pending_today(request)
        comment = self.comment_box.get("1.0", "end-1c").strip()
        if not approve and not comment:
            raise ValueError("Enter a comment explaining the rejection.")
        action = "Approve" if approve else "Reject"
        if not messagebox.askyesno(action + " OT", f"{action} request #{request.id} for {request.employee_name}?\n"
                                   f"{request.start_time:%H:%M}–{request.end_time:%H:%M} | "
                                   f"{duration_text(request.duration_minutes)}", parent=self):
            return
        self.service.decide(request, approve, comment)
        messagebox.showinfo("Decision saved", f"Request #{request.id} {'approved' if approve else 'rejected'}.", parent=self)
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
            f"Time: {request.start_time:%H:%M} to {request.end_time:%H:%M}{next_day}\n"
            f"Duration: {duration_text(request.duration_minutes)}\n\nREASON\n{request.reason}\n\n"
            f"Approver: {request.approver_id or '—'}\nComment: {request.approver_comment or '—'}\n"
            f"Decision time: {request.decided_at or '—'}\n\nAUDIT HISTORY\n"
        )
        for timestamp, actor, action, details in audit:
            content += f"\n{timestamp} | {actor} | {action}\n{details or ''}\n"
        text.insert("1.0", content)
        text.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=8)


if __name__ == "__main__":
    raise SystemExit("Import OvertimeWindow into your existing Tkinter application; see example_integration.py.")
