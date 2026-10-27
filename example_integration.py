"""Integration with the user's existing shared_var.shared_variable and Tk app.

Do not run this as a separate app. Use add_ot_button() in the current app after
login. Neither shared_var.py nor pg_util.py is replaced or recreated here.
"""
from collections.abc import Callable, Sequence
from tkinter import messagebox, ttk

import shared_var
from overtime_management import OvertimeWindow

# Populate ONLY with exact role values that your own application authorizes for
# OT approval. Intentionally empty: no unknown role receives approval rights.
# For example, if your real OT-approver role is "OT_APPROVER", use:
# OT_APPROVER_ROLES = frozenset({"OT_APPROVER"})
OT_APPROVER_ROLES: frozenset[str] = frozenset()


def can_current_user_approve_ot() -> bool:
    """Replace with your existing trusted permission check when available."""
    role = shared_var.shared_variable.role
    return isinstance(role, str) and role in OT_APPROVER_ROLES


def add_ot_button(
    parent,
    *,
    permission_check: Callable[[], bool] = can_current_user_approve_ot,
    approval_departments: Sequence[str] | None = None,
):
    """Return an unpacked button; permission is checked on every click.

    None for approval_departments means the logged-in employee's department.
    Keep your existing root/mainloop. Close OT windows on logout/role revocation.
    This desktop-side check is not a substitute for server-side authorization.
    """
    window = None

    def open_ot_window():
        nonlocal window
        try:
            can_approve = permission_check()
            if type(can_approve) is not bool:
                raise ValueError("The OT permission check must return True or False.")
            if window is not None and window.winfo_exists():
                try:
                    window.validate_session()
                    if window.service.is_approver != can_approve:
                        raise PermissionError("OT approval permission changed.")
                except (ValueError, PermissionError):
                    window.destroy()
                    window = None
                else:
                    window.deiconify()
                    window.lift()
                    window.focus_set()
                    return
            window = OvertimeWindow(
                master=parent,
                # Passing the MODULE lets session checks also notice replacement
                # of shared_var.shared_variable during login/logout.
                shared_vars=shared_var,
                is_approver=can_approve,
                approval_departments=approval_departments,
            )
        except Exception as exc:
            messagebox.showerror("Cannot open Overtime Management", str(exc), parent=parent)

    return ttk.Button(parent, text="Overtime Management", command=open_ot_window)


# Inside your EXISTING UI setup, after authenticated login:
#
# from example_integration import add_ot_button
# button = add_ot_button(root)
# button.pack(padx=10, pady=10)
#
# Or supply your existing trusted permission callback:
# button = add_ot_button(root, permission_check=your_ot_permission_check,
#                        approval_departments=["Production", "Maintenance"])
# button.pack()
#
# Do not create SharedVariable() again. Reuse shared_var.shared_variable.
