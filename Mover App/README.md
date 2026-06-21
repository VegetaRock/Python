# Reusable PySide6 verified file mover

Use `FileMoveDialog` inside an existing PySide6 application. Your software supplies:

- the complete source project folder, for example `Dir1/123456`;
- the destination root, for example `Dir2`.

The resulting destination is `Dir2/123456`.

The dialog starts the operation after it is displayed and shows the current file operation, overall progress, and a timestamped log. The log includes path validation, scans, lock-file skips, staging, copy/replace operations, SHA-256 verification, TRASH/OTHER randomization, rename-log creation, destination commit, source cleanup, leftovers, and PostgreSQL or fallback logging.

## Add it to your software

Keep these files in the same Python package/module directory:

```text
mover_core.py
file_move_dialog.py
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Call the dialog from a slot or method in your existing PySide6 window:

```python
from file_move_dialog import FileMoveDialog


def move_folder(self, folder_path: str, destination_dir: str) -> None:
    dialog = FileMoveDialog(
        source_folder=folder_path,       # e.g. C:/Dir1/123456
        destination_dir=destination_dir, # e.g. D:/Dir2
        parent=self,
        postgres_dsn=(
            "postgresql://user:password@server:5432/database"
        ),
        keep_destination_backup=True,
    )

    # The file work runs in QThread, so the dialog remains responsive.
    dialog.exec()

    if dialog.move_result is not None:
        result = dialog.move_result
        print(result.status)
        print(result.destination_project)
    elif dialog.error_message:
        print(dialog.error_message)
        print(dialog.error_details)
    elif dialog.cancelled_message:
        print(dialog.cancelled_message)
```

Do not create another `QApplication`; use the one already running in your software.

For non-modal use, keep the dialog as an instance attribute so Python does not destroy it while it is running:

```python
self.file_move_dialog = FileMoveDialog(
    source_folder=folder_path,
    destination_dir=destination_dir,
    parent=self,
    postgres_dsn=postgres_dsn,
)
self.file_move_dialog.move_succeeded.connect(self.on_move_completed)
self.file_move_dialog.move_failed.connect(self.on_move_failed)
self.file_move_dialog.show()
```

`integration_example.py` contains both patterns.

## Processing sequence

1. Validate source and destination paths and acquire a per-project run lock.
2. Scan the source while excluding configured lock files.
3. Clone the existing destination project to a staging folder, when present.
4. Overlay the source into staging. A source file replaces a same-path destination file; destination-only files remain.
5. Randomize every descendant name in top-level matching folders whose names contain `TRASH` or `OTHER`, case-insensitively. File extensions can be retained.
6. Write a JSON Lines rename log inside every matched folder.
7. Verify the source snapshot and staging copy with SHA-256.
8. Atomically commit staging. The former destination is kept as a timestamped backup by default.
9. Verify the committed destination again.
10. Delete only verified source data. Configured lock files are neither copied nor deleted.
11. When the source folder remains, record its folder name, full path, and leftover details in PostgreSQL. If PostgreSQL is unavailable or no DSN is supplied, write a fallback JSON incident log in the destination.

## Dialog behavior

The window cannot be closed while the worker is active. Cancellation is accepted before destination commit. Once commit begins, cancellation is disabled so verification and cleanup can leave the operation in a consistent state. The dialog remains open after completion, failure, or cancellation so the user can review the full operation log.

The following result properties are available after the dialog closes:

```python
dialog.move_result       # MoveResult on success
dialog.error_message     # short failure description
dialog.error_details     # traceback/details
dialog.cancelled_message # cancellation description
```

The dialog also emits:

```text
move_succeeded(MoveResult)
move_failed(message, details)
move_cancelled(message)
```

## Default lock-file patterns

```text
*.lock
*.lck
~$*
.~lock.*#
```

Pass custom patterns through `lock_patterns=` when constructing the dialog.

## Safety boundary

Keep both source and destination quiescent during the run. The mover detects ordinary changes and refuses unsafe source deletion, but portable Python code cannot prevent another process from changing a file at the exact instant before deletion. For data that is actively being written, stop the producing process or move from a filesystem snapshot.

Regular-file contents are verified with SHA-256. Standard Python copy APIs do not guarantee preservation of every platform-specific metadata feature, such as all ACLs, owners, Windows alternate data streams, or every extended attribute.
