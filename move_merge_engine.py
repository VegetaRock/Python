from __future__ import annotations

import csv
import hashlib
import os
import shutil
import stat
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional


# A fixed namespace makes the "random-looking" renamed file names deterministic.
# That is important for safe retries: the same source file gets the same UUID name.
_RENAME_NAMESPACE = uuid.UUID("42ce73f1-e52e-4c18-b4d8-1a36ad21a647")
_DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
_MIN_FREE_SPACE_MARGIN = 64 * 1024 * 1024


class MoveMergeError(RuntimeError):
    """Base exception for the move/merge operation."""


class OperationCancelled(MoveMergeError):
    """Raised when cancellation is requested before the commit is finalized."""


class SourceChangedError(MoveMergeError):
    """Raised when a source file changes while the safe snapshot is being made."""


class DestinationConflictError(MoveMergeError):
    """Raised when the destination has an unsafe file/folder conflict."""


class RollbackError(MoveMergeError):
    """Raised when a destination rollback cannot be completed fully."""


@dataclass
class SourceItem:
    source_path: Path
    source_rel: Path
    size: int
    mtime_ns: int
    mode: int
    device: int
    inode: int
    special_root: Optional[Path]
    sha256: str = ""
    destination_rel: Optional[Path] = None
    staged_path: Optional[Path] = None


@dataclass
class PayloadItem:
    staged_path: Path
    destination_rel: Path
    size: int
    sha256: str
    source_item: Optional[SourceItem] = None
    generated_kind: str = "source"


@dataclass
class ChangeRecord:
    destination_path: Path
    existed_before: bool
    backup_path: Optional[Path]
    action: str
    original_mode: Optional[int] = None
    original_size: Optional[int] = None
    original_mtime_ns: Optional[int] = None
    original_device: Optional[int] = None
    original_inode: Optional[int] = None
    applied: bool = False


@dataclass
class MoveMergeResult:
    status: str
    source_folder: str
    destination_folder: str
    sheet_name: str
    total_source_files: int
    total_source_bytes: int
    renamed_files: int
    copied_files: int
    deleted_source_files: int
    source_folder_removed: bool
    cancellation_requested_after_commit: bool
    remaining_item_count: int
    remaining_items: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class MoveMergeHooks:
    """
    Replace the bodies of these two methods with your PostgreSQL code later.

    The engine calls only one method when the source folder could not be removed:
      - sheet_name == "R(0)" -> log_m_postgress(...)
      - every other sheet name -> log_cn_postgress(...)
    """

    def log_m_postgress(
        self,
        *,
        folder_name: str,
        folder_path: str,
        destination_path: str,
        sheet_name: str,
        remaining_items: list[str],
        remaining_item_count: int,
    ) -> None:
        # TODO: Write your PostgreSQL INSERT/UPDATE here.
        pass

    def log_cn_postgress(
        self,
        *,
        folder_name: str,
        folder_path: str,
        destination_path: str,
        sheet_name: str,
        remaining_items: list[str],
        remaining_item_count: int,
    ) -> None:
        # TODO: Write your PostgreSQL INSERT/UPDATE here.
        pass


class InterProcessFileLock:
    """A small cross-platform non-blocking file lock.

    The lock file remains on disk, but the operating-system lock is released
    automatically when the process exits or crashes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> "InterProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise MoveMergeError(
                f"Another move/merge operation is already using this folder: {self.path}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class SafeMoveMergeEngine:
    """
    Transaction-style folder move and merge engine.

    Safety rules used by this class:
      1. The source is never deleted until every source file exists and is
         SHA-256 verified at the destination.
      2. Files are copied to a staging folder first.
      3. Existing destination files are copied to a verified rollback backup
         before they are replaced or removed.
      4. The destination commit is rolled back if copying, merging, verification,
         or cancellation fails before the commit is finalized.
      5. A source file is deleted only after both source and destination are
         hashed again immediately before deletion.
      6. Locked, changed, new, or otherwise undeletable source files are left in
         place and sent to the appropriate PostgreSQL hook.

    The final destination folder is:
        destination_root / source_folder.name
    """

    def __init__(
        self,
        source_folder: str | os.PathLike[str],
        destination_root: str | os.PathLike[str],
        sheet_name: str,
        *,
        hooks: Optional[MoveMergeHooks] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
        on_stage: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        strict_verification: bool = True,
        special_folder_names: Iterable[str] = ("TRASH", "OTHER"),
    ) -> None:
        self.source_folder = Path(os.path.abspath(os.fspath(source_folder)))
        self.destination_root = Path(os.path.abspath(os.fspath(destination_root)))
        self.sheet_name = str(sheet_name)
        self.destination_folder = self.destination_root / self.source_folder.name

        self.hooks = hooks or MoveMergeHooks()
        self.on_log = on_log or (lambda _message: None)
        self.on_progress = on_progress or (lambda _value: None)
        self.on_stage = on_stage or (lambda _stage: None)
        self.cancel_event = cancel_event or threading.Event()
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.strict_verification = bool(strict_verification)
        self.special_folder_names = {
            str(name).strip().upper() for name in special_folder_names if str(name).strip()
        }

        self._last_progress = -1
        self._warnings: list[str] = []
        self._job_id = (
            datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        )
        self._stage_root = self.destination_root / (
            f".{self.source_folder.name}.move_merge_stage_{self._job_id}"
        )
        self._backup_root = self.destination_root / (
            f".{self.source_folder.name}.move_merge_backup_{self._job_id}"
        )
        self._lock_path = self.destination_root / (
            f".{self.source_folder.name}.move_merge.lock"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def request_cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> MoveMergeResult:
        self._emit_progress(0)
        self._validate_paths()
        self.destination_root.mkdir(parents=True, exist_ok=True)

        with InterProcessFileLock(self._lock_path):
            return self._run_locked()

    # ------------------------------------------------------------------
    # Main workflow
    # ------------------------------------------------------------------
    def _run_locked(self) -> MoveMergeResult:
        source_items: list[SourceItem] = []
        source_dirs: list[Path] = []
        payload_items: list[PayloadItem] = []
        cleanup_original_rels: set[Path] = set()
        changes: list[ChangeRecord] = []
        created_destination_dirs: list[Path] = []
        rollback_completed = True
        commit_finalized = False
        total_bytes = 0
        renamed_count = 0
        deleted_count = 0
        cancelled_after_commit = False

        try:
            self._set_stage("Scanning source folder")
            self._emit_log(f"Source: {self.source_folder}")
            self._emit_log(f"Destination: {self.destination_folder}")
            source_items, source_dirs = self._scan_source()
            total_bytes = sum(item.size for item in source_items)
            self._emit_log(
                f"Scan complete: {len(source_items)} file(s), "
                f"{self._format_bytes(total_bytes)}."
            )
            self._emit_progress(3)
            self._check_cancel()

            self._check_free_space_for_staging(total_bytes)
            self._stage_root.mkdir(parents=True, exist_ok=False)

            self._set_stage("Copying source to verified staging area")
            payload_items = self._copy_all_to_staging(source_items, total_bytes)
            renamed_count = sum(1 for item in source_items if item.special_root is not None)

            self._set_stage("Verifying source snapshot before merge")
            self._verify_source_snapshot(source_items, progress_start=45, progress_end=55)

            self._set_stage("Generating TRASH/OTHER rename logs")
            generated_logs = self._create_rename_logs(source_items, payload_items)
            payload_items.extend(generated_logs)

            payload_destination_keys = {
                self._relative_key(item.destination_rel) for item in payload_items
            }
            cleanup_original_rels = {
                item.source_rel
                for item in source_items
                if item.special_root is not None
                and self._relative_key(item.source_rel) not in payload_destination_keys
            }

            self._preflight_destination(
                source_dirs=source_dirs,
                payload_items=payload_items,
                cleanup_original_rels=cleanup_original_rels,
            )
            self._check_free_space_for_backups(payload_items, cleanup_original_rels)
            self._check_cancel()

            self._set_stage("Merging into destination with rollback protection")
            self._commit_destination(
                source_dirs=source_dirs,
                payload_items=payload_items,
                cleanup_original_rels=cleanup_original_rels,
                changes=changes,
                created_dirs=created_destination_dirs,
            )

            self._set_stage("Verifying committed destination")
            self._verify_committed_destination(
                payload_items,
                cleanup_original_rels,
                progress_start=70,
                progress_end=78,
            )

            # Check again after the destination commit. If any source file changed
            # during the commit, restore the old destination and leave source intact.
            self._set_stage("Rechecking source after destination commit")
            self._verify_source_snapshot(source_items, progress_start=78, progress_end=86)

            commit_finalized = True
            self._emit_log(
                "Destination commit is fully verified. Source deletion may now begin."
            )

        except Exception as original_error:
            if changes or created_destination_dirs:
                try:
                    self._rollback_destination(changes, created_destination_dirs)
                except Exception as rollback_error:
                    rollback_completed = False
                    self._emit_log(f"CRITICAL: rollback problem: {rollback_error}")
                    self._warnings.append(str(rollback_error))

            if rollback_completed:
                self._safe_remove_tree(self._stage_root)
                self._safe_remove_tree(self._backup_root)
            else:
                self._emit_log(
                    "Rollback files were kept for manual recovery:\n"
                    f"  Stage: {self._stage_root}\n"
                    f"  Backup: {self._backup_root}"
                )

            if isinstance(original_error, OperationCancelled):
                raise
            if not rollback_completed:
                raise RollbackError(
                    f"Original error: {original_error}. Destination rollback was not "
                    f"fully completed. Recovery backup: {self._backup_root}"
                ) from original_error
            raise

        if not commit_finalized:
            # Defensive guard; normally unreachable.
            raise MoveMergeError("Destination commit was not finalized.")

        # From this point onward, the destination is a complete verified copy.
        # We do not roll it back because source deletion may become partial.
        self._set_stage("Deleting verified source files")
        try:
            deleted_count, cancelled_after_commit = self._delete_source_safely(
                source_items,
                progress_start=86,
                progress_end=99,
            )
        except Exception as exc:
            # A deletion-stage failure must never damage the verified destination.
            warning = f"Source cleanup stopped safely: {exc}"
            self._warnings.append(warning)
            self._emit_log(f"WARNING: {warning}")

        self._remove_empty_source_directories()
        source_removed = not os.path.lexists(self.source_folder)
        remaining_items = [] if source_removed else self._collect_remaining_items()

        if source_removed:
            self._emit_log("Source folder removed completely.")
        else:
            self._emit_log(
                f"Source folder still contains {len(remaining_items)} displayed "
                "item(s). Locked, changed, or newly created items were not deleted."
            )
            self._call_postgres_hook(remaining_items)

        self._safe_remove_tree(self._stage_root)
        self._safe_remove_tree(self._backup_root)
        self._emit_progress(100)

        if cancelled_after_commit:
            status = "cancelled_after_safe_copy"
        elif source_removed:
            status = "completed"
        else:
            status = "completed_with_leftovers"

        return MoveMergeResult(
            status=status,
            source_folder=str(self.source_folder),
            destination_folder=str(self.destination_folder),
            sheet_name=self.sheet_name,
            total_source_files=len(source_items),
            total_source_bytes=total_bytes,
            renamed_files=renamed_count,
            copied_files=len(source_items),
            deleted_source_files=deleted_count,
            source_folder_removed=source_removed,
            cancellation_requested_after_commit=cancelled_after_commit,
            remaining_item_count=self._count_remaining_items(),
            remaining_items=remaining_items,
            warnings=list(self._warnings),
        )

    # ------------------------------------------------------------------
    # Validation and scanning
    # ------------------------------------------------------------------
    def _validate_paths(self) -> None:
        if not self.source_folder.exists() or not self.source_folder.is_dir():
            raise MoveMergeError(f"Source folder does not exist: {self.source_folder}")
        if self._is_link_or_junction(self.source_folder):
            raise MoveMergeError("The source folder cannot be a symlink or junction.")

        source_real = Path(os.path.realpath(self.source_folder))
        destination_real = Path(os.path.realpath(self.destination_root))
        target_real = destination_real / self.source_folder.name

        if self._same_path(source_real, target_real):
            raise MoveMergeError("Source and destination folders are the same.")
        if self._is_relative_to(target_real, source_real):
            raise MoveMergeError("Destination cannot be inside the source folder.")
        if self._is_relative_to(source_real, target_real):
            raise MoveMergeError("Source cannot be inside the destination target folder.")

        existing_parent = self.destination_root
        while not existing_parent.exists() and existing_parent != existing_parent.parent:
            existing_parent = existing_parent.parent
        if existing_parent.exists() and self._is_link_or_junction(existing_parent):
            raise MoveMergeError(
                f"Destination path uses a symlink/junction: {existing_parent}"
            )

    def _scan_source(self) -> tuple[list[SourceItem], list[Path]]:
        files: list[SourceItem] = []
        directories: list[Path] = []

        def recurse(current: Path, current_rel: Path) -> None:
            self._check_cancel()
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name.casefold())
            except OSError as exc:
                raise MoveMergeError(f"Cannot scan folder: {current}: {exc}") from exc

            for entry in entries:
                self._check_cancel()
                path = Path(entry.path)
                rel = current_rel / entry.name if current_rel != Path(".") else Path(entry.name)

                try:
                    is_link = entry.is_symlink() or self._is_link_or_junction(path)
                except OSError as exc:
                    raise MoveMergeError(f"Cannot inspect path: {path}: {exc}") from exc
                if is_link:
                    raise MoveMergeError(
                        f"Symlink/junction found and not processed for safety: {path}"
                    )

                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(rel)
                        recurse(path, rel)
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(st.st_mode):
                            raise MoveMergeError(
                                f"Unsupported non-regular file found: {path}"
                            )
                        files.append(
                            SourceItem(
                                source_path=path,
                                source_rel=rel,
                                size=st.st_size,
                                mtime_ns=st.st_mtime_ns,
                                mode=st.st_mode,
                                device=st.st_dev,
                                inode=st.st_ino,
                                special_root=self._find_special_root(rel),
                            )
                        )
                    else:
                        raise MoveMergeError(
                            f"Unsupported filesystem item found: {path}"
                        )
                except OSError as exc:
                    raise MoveMergeError(f"Cannot inspect path: {path}: {exc}") from exc

        recurse(self.source_folder, Path("."))
        return files, directories

    # ------------------------------------------------------------------
    # Staging and rename-log generation
    # ------------------------------------------------------------------
    def _copy_all_to_staging(
        self, source_items: list[SourceItem], total_bytes: int
    ) -> list[PayloadItem]:
        payload_items: list[PayloadItem] = []
        destination_keys: set[str] = set()
        incoming_root = self._stage_root / ".incoming"
        copy_total_units = max(1, (2 * total_bytes) + len(source_items))
        copy_done_units = 0

        def advance_copy(byte_count: int) -> None:
            nonlocal copy_done_units
            copy_done_units += max(0, byte_count)
            self._emit_progress_in_range(3, 45, copy_done_units, copy_total_units)

        for index, item in enumerate(source_items, start=1):
            self._check_cancel()
            self._emit_log(
                f"Copying [{index}/{len(source_items)}]: {item.source_rel.as_posix()}"
            )

            if item.special_root is None:
                stage_final = self._stage_root / item.source_rel
                copied_temp, source_hash = self._copy_file_snapshot(
                    item, stage_final, advance_copy
                )
                destination_rel = item.source_rel
            else:
                incoming_root.mkdir(parents=True, exist_ok=True)
                provisional = incoming_root / f"{index:08d}_{uuid.uuid4().hex}.data"
                copied_temp, source_hash = self._copy_file_snapshot(
                    item, provisional, advance_copy
                )
                destination_rel = self._make_deterministic_renamed_path(
                    item.source_rel
                )
                stage_final = self._stage_root / destination_rel
                stage_final.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(stage_final):
                    raise MoveMergeError(
                        f"Staging name collision detected: {destination_rel}"
                    )
                os.replace(copied_temp, stage_final)
                self._fsync_directory(stage_final.parent)
                copied_temp = stage_final
                self._emit_log(
                    "Rename planned: "
                    f"{item.source_rel.as_posix()} -> {destination_rel.as_posix()}"
                )

            key = self._relative_key(destination_rel)
            if key in destination_keys:
                raise MoveMergeError(
                    f"Two source files map to the same destination path: {destination_rel}"
                )
            destination_keys.add(key)

            item.sha256 = source_hash
            item.destination_rel = destination_rel
            item.staged_path = copied_temp
            payload_items.append(
                PayloadItem(
                    staged_path=copied_temp,
                    destination_rel=destination_rel,
                    size=item.size,
                    sha256=source_hash,
                    source_item=item,
                )
            )
            copy_done_units += 1
            self._emit_progress_in_range(3, 45, copy_done_units, copy_total_units)
            self._emit_log(
                f"Verified staged copy: {destination_rel.as_posix()} "
                f"(SHA-256 {source_hash[:12]}...)"
            )

        self._emit_progress(45)
        return payload_items

    def _copy_file_snapshot(
        self,
        item: SourceItem,
        requested_path: Path,
        progress_callback: Callable[[int], None],
    ) -> tuple[Path, str]:
        self._assert_source_metadata_unchanged(item)
        requested_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = requested_path.with_name(
            f".{requested_path.name}.{uuid.uuid4().hex}.part"
        )
        source_hash = hashlib.sha256()

        try:
            with open(item.source_path, "rb") as source_file, open(temp_path, "xb") as dest_file:
                while True:
                    self._check_cancel()
                    chunk = source_file.read(self.chunk_size)
                    if not chunk:
                        break
                    dest_file.write(chunk)
                    source_hash.update(chunk)
                    progress_callback(len(chunk))
                dest_file.flush()
                os.fsync(dest_file.fileno())

            self._assert_source_metadata_unchanged(item)
            try:
                shutil.copystat(item.source_path, temp_path, follow_symlinks=False)
            except OSError as exc:
                self._warn(
                    f"Could not copy all metadata for {item.source_rel.as_posix()}: {exc}"
                )

            digest = source_hash.hexdigest()
            staged_digest, staged_size = self._hash_file(
                temp_path,
                progress_callback=progress_callback,
                check_cancel=True,
            )
            if staged_size != item.size or staged_digest != digest:
                raise MoveMergeError(
                    f"Staging verification failed for {item.source_rel.as_posix()}"
                )

            if requested_path.parent == temp_path.parent and requested_path != temp_path:
                os.replace(temp_path, requested_path)
                self._fsync_directory(requested_path.parent)
                return requested_path, digest
            return temp_path, digest
        except Exception:
            self._safe_unlink(temp_path)
            raise

    def _create_rename_logs(
        self,
        source_items: list[SourceItem],
        existing_payload: list[PayloadItem],
    ) -> list[PayloadItem]:
        groups: dict[Path, list[SourceItem]] = {}
        for item in source_items:
            if item.special_root is not None:
                groups.setdefault(item.special_root, []).append(item)

        if not groups:
            self._emit_log("No TRASH or OTHER files were found; no rename log required.")
            return []

        used_keys = {
            self._relative_key(item.destination_rel) for item in existing_payload
        }
        generated: list[PayloadItem] = []
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")

        for special_root, items in sorted(groups.items(), key=lambda pair: pair[0].as_posix()):
            mapping_text = "\n".join(
                f"{item.source_rel.as_posix()}|{item.destination_rel.as_posix()}|{item.sha256}"
                for item in sorted(items, key=lambda value: value.source_rel.as_posix())
            )
            mapping_digest = hashlib.sha256(mapping_text.encode("utf-8")).hexdigest()
            log_rel = special_root / f"rename_log_{mapping_digest[:12]}.csv"
            if self._relative_key(log_rel) in used_keys:
                log_rel = special_root / f"_move_merge_rename_log_{mapping_digest[:16]}.csv"
            if self._relative_key(log_rel) in used_keys:
                raise MoveMergeError(f"Rename-log path collision: {log_rel}")
            used_keys.add(self._relative_key(log_rel))

            log_path = self._stage_root / log_rel
            log_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = log_path.with_name(f".{log_path.name}.{uuid.uuid4().hex}.part")

            with open(temp_path, "w", encoding="utf-8-sig", newline="") as csv_file:
                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=[
                        "created_at",
                        "source_folder",
                        "special_folder",
                        "original_name",
                        "new_name",
                        "original_relative_path",
                        "renamed_relative_path",
                        "size_bytes",
                        "sha256",
                    ],
                )
                writer.writeheader()
                for item in sorted(items, key=lambda value: value.source_rel.as_posix()):
                    assert item.destination_rel is not None
                    writer.writerow(
                        {
                            "created_at": created_at,
                            "source_folder": self.source_folder.name,
                            "special_folder": special_root.as_posix(),
                            "original_name": item.source_rel.name,
                            "new_name": item.destination_rel.name,
                            "original_relative_path": item.source_rel.as_posix(),
                            "renamed_relative_path": item.destination_rel.as_posix(),
                            "size_bytes": item.size,
                            "sha256": item.sha256,
                        }
                    )
                csv_file.flush()
                os.fsync(csv_file.fileno())

            os.replace(temp_path, log_path)
            self._fsync_directory(log_path.parent)
            log_hash, log_size = self._hash_file(log_path, check_cancel=True)
            generated.append(
                PayloadItem(
                    staged_path=log_path,
                    destination_rel=log_rel,
                    size=log_size,
                    sha256=log_hash,
                    source_item=None,
                    generated_kind="rename_log",
                )
            )
            self._emit_log(f"Rename log created: {log_rel.as_posix()}")

        return generated

    # ------------------------------------------------------------------
    # Destination preflight, commit, verification, rollback
    # ------------------------------------------------------------------
    def _preflight_destination(
        self,
        *,
        source_dirs: list[Path],
        payload_items: list[PayloadItem],
        cleanup_original_rels: set[Path],
    ) -> None:
        self._set_stage("Checking destination for conflicts")

        if os.path.lexists(self.destination_folder):
            if self._is_link_or_junction(self.destination_folder):
                raise DestinationConflictError(
                    f"Destination is a symlink/junction: {self.destination_folder}"
                )
            if not self.destination_folder.is_dir():
                raise DestinationConflictError(
                    f"A file already uses the destination folder path: "
                    f"{self.destination_folder}"
                )

        required_dirs: set[Path] = set(source_dirs)
        for payload in payload_items:
            parent = payload.destination_rel.parent
            while parent != Path("."):
                required_dirs.add(parent)
                parent = parent.parent

        directory_keys: set[str] = set()
        for rel_dir in required_dirs:
            key = self._relative_key(rel_dir)
            if key in directory_keys:
                raise DestinationConflictError(
                    f"Case-insensitive duplicate source directory: {rel_dir}"
                )
            directory_keys.add(key)

        for rel_dir in sorted(required_dirs, key=lambda path: (len(path.parts), path.as_posix())):
            destination_dir = self.destination_folder / rel_dir
            if os.path.lexists(destination_dir):
                if self._is_link_or_junction(destination_dir) or not destination_dir.is_dir():
                    raise DestinationConflictError(
                        f"Destination folder conflict: {destination_dir}"
                    )

        all_keys: set[str] = set()
        for payload in payload_items:
            key = self._relative_key(payload.destination_rel)
            if key in all_keys:
                raise DestinationConflictError(
                    f"Duplicate destination path: {payload.destination_rel}"
                )
            all_keys.add(key)
            destination_file = self.destination_folder / payload.destination_rel
            if os.path.lexists(destination_file):
                if self._is_link_or_junction(destination_file) or not destination_file.is_file():
                    raise DestinationConflictError(
                        f"Destination file conflict: {destination_file}"
                    )

        for rel in cleanup_original_rels:
            destination_file = self.destination_folder / rel
            if os.path.lexists(destination_file):
                if self._is_link_or_junction(destination_file) or not destination_file.is_file():
                    raise DestinationConflictError(
                        f"Cannot safely remove original TRASH/OTHER name because it is "
                        f"not a regular file: {destination_file}"
                    )

        self._emit_log("Destination preflight passed.")

    def _commit_destination(
        self,
        *,
        source_dirs: list[Path],
        payload_items: list[PayloadItem],
        cleanup_original_rels: set[Path],
        changes: list[ChangeRecord],
        created_dirs: list[Path],
    ) -> None:

        required_dirs: set[Path] = set(source_dirs)
        for payload in payload_items:
            parent = payload.destination_rel.parent
            while parent != Path("."):
                required_dirs.add(parent)
                parent = parent.parent

        if not self.destination_folder.exists():
            self.destination_folder.mkdir(parents=False, exist_ok=False)
            created_dirs.append(self.destination_folder)
            self._emit_log(f"Created destination folder: {self.destination_folder}")

        for rel_dir in sorted(required_dirs, key=lambda path: (len(path.parts), path.as_posix())):
            destination_dir = self.destination_folder / rel_dir
            if not destination_dir.exists():
                destination_dir.mkdir(exist_ok=False)
                created_dirs.append(destination_dir)
                self._emit_log(f"Created directory: {destination_dir}")

        total_actions = max(1, len(payload_items) + len(cleanup_original_rels))
        completed_actions = 0

        for payload in sorted(payload_items, key=lambda item: item.destination_rel.as_posix()):
            self._check_cancel()
            destination_file = self.destination_folder / payload.destination_rel
            record = self._prepare_destination_change(destination_file, action="replace")
            changes.append(record)
            try:
                self._check_cancel()
                self._assert_destination_snapshot_unchanged(record)
                self._make_writable_if_readonly(destination_file)
                os.replace(payload.staged_path, destination_file)
                record.applied = True
                self._fsync_directory(destination_file.parent)
            except Exception:
                self._restore_mode_if_possible(destination_file, record.original_mode)
                raise
            completed_actions += 1
            self._emit_progress_in_range(
                55, 70, completed_actions, total_actions
            )
            verb = "Replaced" if record.existed_before else "Added"
            self._emit_log(f"{verb}: {payload.destination_rel.as_posix()}")

        payload_keys = {
            self._relative_key(payload.destination_rel) for payload in payload_items
        }
        for rel in sorted(cleanup_original_rels, key=lambda path: path.as_posix()):
            self._check_cancel()
            if self._relative_key(rel) in payload_keys:
                continue
            destination_file = self.destination_folder / rel
            if os.path.lexists(destination_file):
                record = self._prepare_destination_change(destination_file, action="remove")
                changes.append(record)
                try:
                    self._check_cancel()
                    self._assert_destination_snapshot_unchanged(record)
                    self._make_writable_if_readonly(destination_file)
                    destination_file.unlink()
                    record.applied = True
                    self._fsync_directory(destination_file.parent)
                except Exception:
                    self._restore_mode_if_possible(destination_file, record.original_mode)
                    raise
                self._emit_log(
                    "Removed original TRASH/OTHER filename after randomized copy: "
                    f"{rel.as_posix()}"
                )
            completed_actions += 1
            self._emit_progress_in_range(
                55, 70, completed_actions, total_actions
            )

        self._emit_progress(70)

    def _prepare_destination_change(self, destination_file: Path, action: str) -> ChangeRecord:
        if not os.path.lexists(destination_file):
            return ChangeRecord(
                destination_path=destination_file,
                existed_before=False,
                backup_path=None,
                action=action,
                original_mode=None,
                original_size=None,
                original_mtime_ns=None,
                original_device=None,
                original_inode=None,
            )

        if self._is_link_or_junction(destination_file) or not destination_file.is_file():
            raise DestinationConflictError(
                f"Unsafe destination item encountered during commit: {destination_file}"
            )

        backup_path = self._backup_root / destination_file.relative_to(self.destination_folder)
        snapshot_stat = self._copy_existing_destination_to_backup(destination_file, backup_path)
        self._emit_log(f"Rollback backup created: {backup_path}")
        return ChangeRecord(
            destination_path=destination_file,
            existed_before=True,
            backup_path=backup_path,
            action=action,
            original_mode=snapshot_stat.st_mode,
            original_size=snapshot_stat.st_size,
            original_mtime_ns=snapshot_stat.st_mtime_ns,
            original_device=snapshot_stat.st_dev,
            original_inode=snapshot_stat.st_ino,
        )

    def _assert_destination_snapshot_unchanged(self, record: ChangeRecord) -> None:
        if not record.existed_before:
            if os.path.lexists(record.destination_path):
                raise MoveMergeError(
                    f"Destination appeared during commit: {record.destination_path}"
                )
            return

        try:
            st = record.destination_path.stat()
        except OSError as exc:
            raise MoveMergeError(
                f"Destination changed after rollback backup was made: "
                f"{record.destination_path}: {exc}"
            ) from exc

        if (
            st.st_size != record.original_size
            or st.st_mtime_ns != record.original_mtime_ns
            or st.st_dev != record.original_device
            or st.st_ino != record.original_inode
        ):
            raise MoveMergeError(
                f"Destination changed after rollback backup was made: "
                f"{record.destination_path}"
            )

    def _copy_existing_destination_to_backup(
        self, source: Path, backup_path: Path
    ) -> os.stat_result:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = backup_path.with_name(
            f".{backup_path.name}.{uuid.uuid4().hex}.part"
        )
        pre_stat = source.stat()
        source_hash = hashlib.sha256()

        try:
            with open(source, "rb") as source_file, open(temp_path, "xb") as backup_file:
                while True:
                    self._check_cancel()
                    chunk = source_file.read(self.chunk_size)
                    if not chunk:
                        break
                    backup_file.write(chunk)
                    source_hash.update(chunk)
                backup_file.flush()
                os.fsync(backup_file.fileno())

            post_stat = source.stat()
            if (
                pre_stat.st_size != post_stat.st_size
                or pre_stat.st_mtime_ns != post_stat.st_mtime_ns
            ):
                raise MoveMergeError(
                    f"Destination file changed while creating rollback backup: {source}"
                )

            try:
                shutil.copystat(source, temp_path, follow_symlinks=False)
            except OSError as exc:
                self._warn(f"Could not preserve backup metadata for {source}: {exc}")

            expected_hash = source_hash.hexdigest()
            backup_hash, backup_size = self._hash_file(
                temp_path, check_cancel=True
            )
            if backup_size != pre_stat.st_size or backup_hash != expected_hash:
                raise MoveMergeError(f"Rollback backup verification failed: {source}")

            if self.strict_verification:
                source_hash_again, source_size_again = self._hash_file(
                    source, check_cancel=True
                )
                if (
                    source_size_again != pre_stat.st_size
                    or source_hash_again != expected_hash
                ):
                    raise MoveMergeError(
                        f"Destination file changed during backup verification: {source}"
                    )

            final_stat = source.stat()
            if (
                final_stat.st_size != pre_stat.st_size
                or final_stat.st_mtime_ns != pre_stat.st_mtime_ns
                or final_stat.st_dev != pre_stat.st_dev
                or final_stat.st_ino != pre_stat.st_ino
            ):
                raise MoveMergeError(
                    f"Destination file changed before commit: {source}"
                )

            os.replace(temp_path, backup_path)
            self._fsync_directory(backup_path.parent)
            return final_stat
        except Exception:
            self._safe_unlink(temp_path)
            raise

    def _verify_committed_destination(
        self,
        payload_items: list[PayloadItem],
        cleanup_original_rels: set[Path],
        *,
        progress_start: int,
        progress_end: int,
    ) -> None:
        total_units = max(1, sum(item.size for item in payload_items) + len(payload_items))
        completed_units = 0

        def advance(byte_count: int) -> None:
            nonlocal completed_units
            completed_units += max(0, byte_count)
            self._emit_progress_in_range(
                progress_start, progress_end, completed_units, total_units
            )

        for payload in payload_items:
            self._check_cancel()
            destination_file = self.destination_folder / payload.destination_rel
            digest, size = self._hash_file(
                destination_file,
                progress_callback=advance,
                check_cancel=True,
            )
            if size != payload.size or digest != payload.sha256:
                raise MoveMergeError(
                    f"Committed destination verification failed: {destination_file}"
                )
            completed_units += 1
            self._emit_progress_in_range(
                progress_start, progress_end, completed_units, total_units
            )

        payload_keys = {
            self._relative_key(payload.destination_rel) for payload in payload_items
        }
        for rel in cleanup_original_rels:
            if self._relative_key(rel) in payload_keys:
                continue
            if os.path.lexists(self.destination_folder / rel):
                raise MoveMergeError(
                    f"Original TRASH/OTHER filename still exists after rename: {rel}"
                )

        self._emit_progress(progress_end)
        self._emit_log("All committed destination files passed SHA-256 verification.")

    def _rollback_destination(
        self,
        changes: list[ChangeRecord],
        created_dirs: list[Path],
    ) -> None:
        self._set_stage("Rolling back destination changes")
        errors: list[str] = []

        for record in reversed(changes):
            if not record.applied:
                continue
            try:
                destination = record.destination_path
                self._make_writable_if_readonly(destination)
                if record.existed_before:
                    if record.backup_path is None or not record.backup_path.exists():
                        raise RollbackError(
                            f"Missing rollback backup for {destination}"
                        )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(record.backup_path, destination)
                    self._fsync_directory(destination.parent)
                    self._emit_log(f"Restored: {destination}")
                else:
                    if os.path.lexists(destination):
                        if destination.is_file() and not self._is_link_or_junction(destination):
                            destination.unlink()
                            self._emit_log(f"Removed newly added file: {destination}")
                        else:
                            raise RollbackError(
                                f"Unexpected item blocks rollback: {destination}"
                            )
            except Exception as exc:
                errors.append(f"{record.destination_path}: {exc}")

        for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                if directory.exists():
                    directory.rmdir()
            except OSError:
                # It may contain a file that could not be restored/removed.
                pass

        if errors:
            raise RollbackError("; ".join(errors))
        self._emit_log("Destination rollback completed successfully.")

    # ------------------------------------------------------------------
    # Source verification and deletion
    # ------------------------------------------------------------------
    def _verify_source_snapshot(
        self,
        source_items: list[SourceItem],
        *,
        progress_start: int,
        progress_end: int,
    ) -> None:
        total_units = max(1, sum(item.size for item in source_items) + len(source_items))
        completed_units = 0

        def advance(byte_count: int) -> None:
            nonlocal completed_units
            completed_units += max(0, byte_count)
            self._emit_progress_in_range(
                progress_start, progress_end, completed_units, total_units
            )

        for item in source_items:
            self._check_cancel()
            self._assert_source_metadata_unchanged(item)
            digest, size = self._hash_file(
                item.source_path,
                progress_callback=advance,
                check_cancel=True,
            )
            self._assert_source_metadata_unchanged(item)
            if size != item.size or digest != item.sha256:
                raise SourceChangedError(
                    f"Source changed after it was staged: {item.source_rel.as_posix()}"
                )
            completed_units += 1
            self._emit_progress_in_range(
                progress_start, progress_end, completed_units, total_units
            )

        self._emit_progress(progress_end)
        self._emit_log("Source snapshot is unchanged and verified.")

    def _delete_source_safely(
        self,
        source_items: list[SourceItem],
        *,
        progress_start: int,
        progress_end: int,
    ) -> tuple[int, bool]:
        total_units = max(
            1,
            (2 * sum(item.size for item in source_items)) + len(source_items),
        )
        completed_units = 0
        deleted_count = 0
        cancelled = False

        def advance(byte_count: int) -> None:
            nonlocal completed_units
            completed_units += max(0, byte_count)
            self._emit_progress_in_range(
                progress_start, progress_end, completed_units, total_units
            )

        for index, item in enumerate(source_items, start=1):
            if self.cancel_event.is_set():
                cancelled = True
                self._emit_log(
                    "Cancellation requested after safe destination commit. "
                    "Remaining source files will be left in place."
                )
                break

            source_file = item.source_path
            destination_file = self.destination_folder / item.destination_rel

            if not os.path.lexists(source_file):
                self._emit_log(
                    f"Source already absent; no deletion needed: {item.source_rel.as_posix()}"
                )
                completed_units += 1
                continue

            try:
                self._assert_source_metadata_unchanged(item)

                destination_stat_before = destination_file.stat()
                destination_hash, destination_size = self._hash_file(
                    destination_file,
                    progress_callback=advance,
                    check_cancel=True,
                )
                if (
                    destination_size != item.size
                    or destination_hash != item.sha256
                ):
                    self._warn(
                        "Source retained because destination verification changed: "
                        f"{item.source_rel.as_posix()}"
                    )
                    completed_units += 1
                    continue

                source_hash, source_size = self._hash_file(
                    source_file,
                    progress_callback=advance,
                    check_cancel=True,
                )
                self._assert_source_metadata_unchanged(item)
                if source_size != item.size or source_hash != item.sha256:
                    self._warn(
                        f"Source retained because it changed: {item.source_rel.as_posix()}"
                    )
                    completed_units += 1
                    continue

                destination_stat_after = destination_file.stat()
                if (
                    destination_stat_before.st_size != destination_stat_after.st_size
                    or destination_stat_before.st_mtime_ns
                    != destination_stat_after.st_mtime_ns
                ):
                    self._warn(
                        "Source retained because destination changed during final check: "
                        f"{item.source_rel.as_posix()}"
                    )
                    completed_units += 1
                    continue

                if self.cancel_event.is_set():
                    cancelled = True
                    self._emit_log(
                        "Cancellation requested during final verification. "
                        "The current source file was retained."
                    )
                    break

                if self._delete_file_with_readonly_retry(source_file):
                    deleted_count += 1
                    self._emit_log(
                        f"Deleted verified source [{index}/{len(source_items)}]: "
                        f"{item.source_rel.as_posix()}"
                    )
                else:
                    self._warn(
                        "Could not delete source (locked or permission denied); retained: "
                        f"{item.source_rel.as_posix()}"
                    )
            except OperationCancelled:
                cancelled = True
                self._emit_log(
                    "Cancellation requested during source cleanup. "
                    "The current and remaining source files were retained."
                )
                break
            except (OSError, MoveMergeError) as exc:
                self._warn(
                    f"Source retained safely: {item.source_rel.as_posix()} ({exc})"
                )

            completed_units += 1
            self._emit_progress_in_range(
                progress_start, progress_end, completed_units, total_units
            )

        self._emit_progress(progress_end)
        return deleted_count, cancelled

    def _remove_empty_source_directories(self) -> None:
        if not self.source_folder.exists():
            return
        try:
            for root, dirnames, _filenames in os.walk(
                self.source_folder, topdown=False, followlinks=False
            ):
                root_path = Path(root)
                for dirname in dirnames:
                    directory = root_path / dirname
                    if self._is_link_or_junction(directory):
                        continue
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
            try:
                self.source_folder.rmdir()
            except OSError:
                pass
        except OSError as exc:
            self._warn(f"Could not finish removing empty source directories: {exc}")

    # ------------------------------------------------------------------
    # PostgreSQL hook and leftovers
    # ------------------------------------------------------------------
    def _call_postgres_hook(self, remaining_items: list[str]) -> None:
        kwargs = {
            "folder_name": self.source_folder.name,
            "folder_path": str(self.source_folder),
            "destination_path": str(self.destination_folder),
            "sheet_name": self.sheet_name,
            "remaining_items": remaining_items,
            "remaining_item_count": self._count_remaining_items(),
        }
        try:
            if self.sheet_name.strip().casefold() == "r(0)".casefold():
                self._emit_log("Calling log_m_postgress(...)")
                self.hooks.log_m_postgress(**kwargs)
            else:
                self._emit_log("Calling log_cn_postgress(...)")
                self.hooks.log_cn_postgress(**kwargs)
        except Exception as exc:
            self._warn(f"PostgreSQL logging hook failed: {exc}")

    def _collect_remaining_items(self, limit: int = 500) -> list[str]:
        if not os.path.lexists(self.source_folder):
            return []
        result: list[str] = []
        try:
            for root, dirnames, filenames in os.walk(
                self.source_folder, topdown=True, followlinks=False
            ):
                root_path = Path(root)
                for dirname in sorted(dirnames, key=str.casefold):
                    path = root_path / dirname
                    rel = path.relative_to(self.source_folder).as_posix()
                    result.append(f"DIR: {rel}")
                    if len(result) >= limit:
                        result.append(f"... output limited to first {limit} items ...")
                        return result
                for filename in sorted(filenames, key=str.casefold):
                    path = root_path / filename
                    rel = path.relative_to(self.source_folder).as_posix()
                    result.append(f"FILE: {rel}")
                    if len(result) >= limit:
                        result.append(f"... output limited to first {limit} items ...")
                        return result
        except OSError as exc:
            result.append(f"ERROR WHILE LISTING: {exc}")
        return result

    def _count_remaining_items(self) -> int:
        if not os.path.lexists(self.source_folder):
            return 0
        count = 0
        try:
            for _root, dirnames, filenames in os.walk(
                self.source_folder, topdown=True, followlinks=False
            ):
                count += len(dirnames) + len(filenames)
        except OSError:
            return max(1, count)
        if count == 0 and os.path.lexists(self.source_folder):
            return 1
        return count

    # ------------------------------------------------------------------
    # Disk-space checks
    # ------------------------------------------------------------------
    def _check_free_space_for_staging(self, total_source_bytes: int) -> None:
        required = total_source_bytes + max(
            _MIN_FREE_SPACE_MARGIN, int(total_source_bytes * 0.05)
        )
        self._ensure_free_space(required, "staging copy")

    def _check_free_space_for_backups(
        self,
        payload_items: list[PayloadItem],
        cleanup_original_rels: set[Path],
    ) -> None:
        rels = {payload.destination_rel for payload in payload_items}
        rels.update(cleanup_original_rels)
        backup_bytes = 0
        for rel in rels:
            path = self.destination_folder / rel
            if os.path.lexists(path) and path.is_file() and not self._is_link_or_junction(path):
                backup_bytes += path.stat().st_size
        required = backup_bytes + max(
            _MIN_FREE_SPACE_MARGIN, int(backup_bytes * 0.05)
        )
        self._ensure_free_space(required, "rollback backup")

    def _ensure_free_space(self, required_bytes: int, purpose: str) -> None:
        try:
            free_bytes = shutil.disk_usage(self.destination_root).free
        except OSError as exc:
            self._warn(f"Could not read destination free space: {exc}")
            return
        if free_bytes < required_bytes:
            raise MoveMergeError(
                f"Not enough free space for {purpose}. Required approximately "
                f"{self._format_bytes(required_bytes)}, available "
                f"{self._format_bytes(free_bytes)}."
            )
        self._emit_log(
            f"Free-space check passed for {purpose}: "
            f"{self._format_bytes(free_bytes)} available."
        )

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------
    def _hash_file(
        self,
        path: Path,
        *,
        progress_callback: Optional[Callable[[int], None]] = None,
        check_cancel: bool = False,
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as file:
            while True:
                if check_cancel:
                    self._check_cancel()
                chunk = file.read(self.chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if progress_callback is not None:
                    progress_callback(len(chunk))
        return digest.hexdigest(), size

    def _assert_source_metadata_unchanged(self, item: SourceItem) -> None:
        try:
            st = item.source_path.stat()
        except OSError as exc:
            raise SourceChangedError(
                f"Source file became unavailable: {item.source_rel.as_posix()}: {exc}"
            ) from exc
        if (
            st.st_size != item.size
            or st.st_mtime_ns != item.mtime_ns
            or st.st_dev != item.device
            or st.st_ino != item.inode
        ):
            raise SourceChangedError(
                f"Source file changed during operation: {item.source_rel.as_posix()}"
            )

    def _delete_file_with_readonly_retry(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            try:
                original_mode = path.stat().st_mode
            except OSError:
                return False
            if original_mode & stat.S_IWUSR:
                return False
            try:
                os.chmod(path, original_mode | stat.S_IWUSR)
                path.unlink()
                return True
            except OSError:
                try:
                    if path.exists():
                        os.chmod(path, original_mode)
                except OSError:
                    pass
                return False
        except OSError:
            return False

    def _make_writable_if_readonly(self, path: Path) -> None:
        if not os.path.lexists(path) or not path.is_file():
            return
        try:
            mode = path.stat().st_mode
            if not (mode & stat.S_IWUSR):
                os.chmod(path, mode | stat.S_IWUSR)
        except OSError:
            # The following replace/unlink will produce the actionable error.
            pass

    @staticmethod
    def _restore_mode_if_possible(path: Path, original_mode: Optional[int]) -> None:
        if original_mode is None or not os.path.lexists(path):
            return
        try:
            os.chmod(path, original_mode)
        except OSError:
            pass

    def _make_deterministic_renamed_path(
        self, original_rel: Path
    ) -> Path:
        # The name is random-looking but stable for a given original path.
        # Therefore a later merge of an updated file replaces the same anonymized
        # destination file instead of creating uncontrolled duplicates.
        token = uuid.uuid5(
            _RENAME_NAMESPACE,
            f"{self.source_folder.name}|{original_rel.as_posix()}",
        ).hex
        extension = original_rel.suffix
        return original_rel.with_name(f"{token}{extension}")

    def _find_special_root(self, file_rel: Path) -> Optional[Path]:
        special_root: Optional[Path] = None
        parent_parts = file_rel.parts[:-1]
        for index, part in enumerate(parent_parts):
            upper = part.upper()
            if any(
                upper == marker or upper.endswith(f"_{marker}")
                for marker in self.special_folder_names
            ):
                special_root = Path(*parent_parts[: index + 1])
        return special_root

    def _is_link_or_junction(self, path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True

            # Python versions before Path.is_junction() still expose the Windows
            # reparse-point attribute. Reject reparse-point directories so a scan
            # can never walk outside the requested source tree.
            if os.name == "nt":
                st = path.lstat()
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                attributes = getattr(st, "st_file_attributes", 0)
                if reparse_flag and (attributes & reparse_flag) and stat.S_ISDIR(st.st_mode):
                    return True
        except OSError:
            return True
        return False

    def _safe_unlink(self, path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            if os.path.lexists(path) and path.is_file():
                self._make_writable_if_readonly(path)
                path.unlink()
        except OSError:
            pass

    def _safe_remove_tree(self, path: Path) -> None:
        if not os.path.lexists(path):
            return

        def onerror(function, failed_path, _exc_info) -> None:
            try:
                os.chmod(failed_path, stat.S_IWUSR)
                function(failed_path)
            except OSError:
                pass

        try:
            shutil.rmtree(path, onerror=onerror)
        except OSError as exc:
            self._warn(f"Could not remove temporary folder {path}: {exc}")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise OperationCancelled(
                "Operation cancelled before source deletion. Source data was kept intact."
            )

    def _set_stage(self, text: str) -> None:
        self.on_stage(text)
        self._emit_log(f"--- {text} ---")

    def _emit_log(self, message: str) -> None:
        self.on_log(str(message))

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        self._emit_log(f"WARNING: {message}")

    def _emit_progress(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if value != self._last_progress:
            self._last_progress = value
            self.on_progress(value)

    def _emit_progress_in_range(
        self,
        start: int,
        end: int,
        completed: int,
        total: int,
    ) -> None:
        if total <= 0:
            self._emit_progress(end)
            return
        ratio = min(1.0, max(0.0, completed / total))
        self._emit_progress(start + int((end - start) * ratio))

    @staticmethod
    def _relative_key(path: Path) -> str:
        # casefold is intentional because the target is normally Windows.
        return path.as_posix().casefold()

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{value} B"

    @staticmethod
    def _same_path(path_a: Path, path_b: Path) -> bool:
        return os.path.normcase(os.path.abspath(path_a)) == os.path.normcase(
            os.path.abspath(path_b)
        )

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

