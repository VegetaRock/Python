"""Copy this integration pattern into your existing application.

Your login code must populate shared_var before open_ot_window() is called.
This file deliberately does not create or overwrite shared_var.py / pg_util.py.
"""
import tkinter as tk
from tkinter import messagebox, ttk

# import shared_var
from overtime_management import OvertimeWindow


def add_ot_button(parent, *, can_approve_ot: bool = False, approval_departments=None):
    """parent is an existing Tk/Toplevel/Frame. Rights come from trusted login code."""
    window = None

    def open_ot_window():
        nonlocal window
        if window is not None and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.focus_set()
            return
        try:
            window = OvertimeWindow(
                master=parent,
                # shared_vars=shared_var,
                field_map={
                    "employee_id": "employee_id",  # Change to your actual ID attribute.
                    "employee_name": "name",       # shared_var.name
                    "department": "department",   # Change if yours is named differently.
                },
                is_approver=can_approve_ot,
                # None defaults to the logged-in employee's department.
                approval_departments=approval_departments,
            )
        except (ValueError, RuntimeError, ImportError) as exc:
            messagebox.showerror("Cannot open Overtime Management", str(exc), parent=parent)

    button = ttk.Button(parent, text="Overtime Management", command=open_ot_window)
    return button


# Inside your existing UI setup, after login:
# button = add_ot_button(root)
# button.pack(padx=10, pady=10)
#
# # When the authenticated user's existing permission system grants OT approval:
# button = add_ot_button(root, can_approve_ot=permission_from_your_login_system,
#                        approval_departments=["Production", "Maintenance"])
# button.pack()

# Keep your application's existing root.mainloop(). Do not create a second root.
# On logout or a role/permission change, close all OT windows and recreate the
# integration for the new authenticated session.

root = tk.Tk()
window = OvertimeWindow(
                master=root,
                # shared_vars=shared_var,
                field_map={
                    "employee_id": "employee_id",  # Change to your actual ID attribute.
                    "employee_name": "name",       # shared_var.name
                    "department": "department",   # Change if yours is named differently.
                },
            )
root.mainloop()

