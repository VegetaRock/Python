from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Literal, Sequence


ProgressCallback = Callable[[int, str], None]
LogCallback = Callable[[str], None]
Kind = Literal["file", "directory", "symlink"]


class MoverError(RuntimeError):
    """Base exception for the safe file mover."""


class OperationCancelled(MoverError):
    """Raised when cancellation is requested before the commit point."""


class SourceChangedError(MoverError):
    """Raised when a file changes while it is being copied or verified."""


class VerificationError(MoverError):
    """Raised when hashes, links, or directory structure do not verify."""


@dataclass(frozen=True)
class MoveOptions:
    source_project: Path
    destination_root: Path
    postgres_dsn: str | None = None
    lock_patterns: tuple[str, ...] = (
        "*.lock",
        "*.lck",
        "~$*",
        ".~lock.*#",
    )
    sensitive_markers: tuple[str, ...] = ("TRASH", "OTHER")
    keep_destination_backup: bool = True
    preserve_file_extension_when_randomizing: bool = True
    durable_writes: bool = True
    copy_retry_count: int = 3
    database_table: str = "file_move_incomplete_cleanup"
    max_leftovers_in_database: int = 10_000
    log_each_item: bool = False


@dataclass
class ScanEntry:
    rel: str
    kind: Kind
    size: int
    mtime_ns: int
    mode: int
    device: int
    inode: int
    link_target: str | None = None


@dataclass
class ManifestRecord:
    rel: str
    final_rel: str
    kind: Kind
    size: int
    mtime_ns: int
    mode: int
    device: int
    inode: int
    sha256: str | None = None
    link_target: str | None = None


@dataclass
class RenamePlan:
    root_rel: str
    log_rel: str
    mapping: dict[str, str]
    renamed_count: int


@dataclass
class Leftover:
    relative_path: str
    kind: str
    reason: str
    error: str | None = None


@dataclass
class MoveResult:
    run_id: str
    status: str
    source_project: str
    destination_project: str
    backup_path: str | None
    journal_path: str
    renamed_logs: list[str] = field(default_factory=list)
    leftovers: list[dict] = field(default_factory=list)
    leftover_count: int = 0
    database_logged: bool = False
    fallback_incident_log: str | None = None
    warnings: list[str] = field(default_factory=list)


class _ProgressTracker:
    def __init__(self, total_work: int, callback: ProgressCallback | None) -> None:
        self.total_work = max(1, total_work)
        self.done = 0
        self.callback = callback
        self.phase = "Starting"
        self.last_percent = -1

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._emit()

    def add(self, amount: int) -> None:
        if amount > 0:
            self.done += amount
        self._emit()

    def finish(self, phase: str = "Completed") -> None:
        self.phase = phase
        if self.callback:
            self.callback(100, self.phase)
        self.last_percent = 100

    def _emit(self) -> None:
        if not self.callback:
            return
        # Reserve 100% for a fully finalized operation. Retries may overrun the estimate.
        percent = min(99, int((self.done * 100) / self.total_work))
        if percent != self.last_percent:
            self.callback(percent, self.phase)
            self.last_percent = percent
        else:
            # Phase changes are useful even if the percentage does not move.
            self.callback(percent, self.phase)


class _RunLock:
    def __init__(self, path: Path, payload: dict) -> None:
        self.path = path
        self.payload = payload
        self.acquired = False

    def __enter__(self) -> "_RunLock":
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise MoverError(
                f"Another run may be active, or a stale run lock exists: {self.path}"
            ) from exc
        try:
            data = json.dumps(self.payload, indent=2, ensure_ascii=False).encode("utf-8")
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


class SafeFileMover:
    """
    Fail-safe directory mover with staging, SHA-256 verification, rollback,
    randomization of content under TRASH/OTHER-style folders, source cleanup,
    and PostgreSQL incident logging.

    The source and destination must remain quiescent during a run. The class
    detects ordinary changes and refuses source cleanup when a change is found,
    but no portable userspace program can prevent an uncooperative process from
    modifying a file in the final instant before deletion.
    """

    COPY_BUFFER_SIZE = 4 * 1024 * 1024

    def __init__(
        self,
        options: MoveOptions,
        *,
        progress_callback: ProgressCallback | None = None,
        log_callback: LogCallback | None = None,
    ) -> None:
        self.options = options
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.cancel_event = threading.Event()
        self.run_id = str(uuid.uuid4())
        self._progress: _ProgressTracker | None = None
        self._committed = False
        self._journal: dict = {}
        self._journal_path: Path | None = None

    def request_cancel(self) -> None:
        """Request cancellation. It is honored before the destination commit point."""
        self.cancel_event.set()

    def move(self) -> MoveResult:
        if self.progress_callback:
            self.progress_callback(0, "Validating paths")
        self._log(f"Starting verified move. Run ID: {self.run_id}")

        source, destination_root, destination = self._validate_and_prepare_paths()
        self._log(f"Source project: {source}")
        self._log(f"Destination root: {destination_root}")
        self._log(f"Final destination project: {destination}")

        run_short = self.run_id.replace("-", "")[:12]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        staging = destination_root / f".{source.name}.staging.{run_short}"
        backup = destination_root / f".{source.name}.backup.{timestamp}.{run_short}"
        lock_key = hashlib.sha256(
            f"{source}|{destination}".encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:16]
        run_lock_path = destination_root / f".file_mover_{lock_key}.lock"
        self._journal_path = destination_root / f".{source.name}.move.{run_short}.journal.json"

        lock_payload = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "source": str(source),
            "destination": str(destination),
            "created_at": self._utc_now(),
        }

        with _RunLock(run_lock_path, lock_payload):
            self._log(f"Run lock acquired: {run_lock_path}")
            if self.progress_callback:
                self.progress_callback(0, "Scanning source project")
            self._log("Scanning source project; configured lock files will be excluded.")
            source_scan = self._scan_tree(source, skip_lock_files=True)

            if destination.exists():
                if self.progress_callback:
                    self.progress_callback(0, "Scanning existing destination")
                self._log(f"Scanning existing destination project: {destination}")
                destination_scan = self._scan_tree(
                    destination, skip_lock_files=False
                )
            else:
                self._log("No existing destination project was found; a new one will be created.")
                destination_scan = []

            source_bytes = sum(e.size for e in source_scan if e.kind == "file")
            destination_bytes = sum(e.size for e in destination_scan if e.kind == "file")
            self._log(
                f"Source scan complete: {len(source_scan)} item(s), "
                f"{self._human_size(source_bytes)} of regular-file data."
            )
            self._log(
                f"Destination scan complete: {len(destination_scan)} item(s), "
                f"{self._human_size(destination_bytes)} of regular-file data."
            )

            # Existing destination clone + source copy + two complete verification rounds.
            total_work = destination_bytes + (5 * source_bytes)
            self._progress = _ProgressTracker(total_work, self.progress_callback)
            self._progress.set_phase("Checking free disk space")
            self._log("Checking destination free space for the staging copy.")
            self._preflight_free_space(destination_root, destination_bytes + source_bytes)
            self._log("Free-space check passed.")

            self._journal = {
                "run_id": self.run_id,
                "status": "preflight_complete",
                "source": str(source),
                "destination": str(destination),
                "staging": str(staging),
                "backup": str(backup) if destination.exists() else None,
                "created_at": self._utc_now(),
                "updated_at": self._utc_now(),
            }
            self._write_journal()

            manifest: dict[str, ManifestRecord] = {}
            rename_plans: list[RenamePlan] = []
            committed_backup: Path | None = None

            try:
                self._check_cancel()
                self._prepare_staging(staging)

                if destination.exists():
                    self._log(f"Cloning current destination into staging: {destination}")
                    self._progress.set_phase("Cloning existing destination")
                    self._copy_tree_from_scan(
                        destination,
                        staging,
                        destination_scan,
                        phase="Cloning existing destination",
                        collect_manifest=False,
                    )
                else:
                    self._copy_root_metadata(source, staging)

                self._check_cancel()
                self._log(f"Overlaying source project into staging: {source}")
                self._progress.set_phase("Copying source project")
                manifest = self._copy_tree_from_scan(
                    source,
                    staging,
                    source_scan,
                    phase="Copying source project",
                    collect_manifest=True,
                )
                self._copy_root_metadata(source, staging)
                self._update_journal(status="source_copied")

                self._check_cancel()
                self._progress.set_phase("Randomizing TRASH/OTHER content")
                rename_plans = self._randomize_sensitive_folders(staging)
                self._apply_rename_mapping_to_manifest(manifest, rename_plans)
                self._update_journal(
                    status="sensitive_content_randomized",
                    rename_logs=[plan.log_rel for plan in rename_plans],
                )

                self._check_cancel()
                self._progress.set_phase("Verifying staged copy")
                source_errors = self._verify_source_snapshot(
                    source, manifest, phase="Verifying source snapshot"
                )
                staged_errors = self._verify_destination_snapshot(
                    staging, manifest, phase="Verifying staged copy"
                )
                if source_errors or staged_errors:
                    raise VerificationError(
                        self._format_verification_errors(source_errors, staged_errors)
                    )

                self._write_audit_manifest(staging, source, destination, manifest, rename_plans)
                self._update_journal(status="verified_before_commit")

                self._check_cancel()
                self._progress.set_phase("Committing destination")
                committed_backup = self._commit(staging, destination, backup)
                self._committed = True
                self._update_journal(
                    status="destination_committed",
                    committed_backup=str(committed_backup) if committed_backup else None,
                )

                # Cancellation is deliberately ignored from here onward: the destination
                # has changed and we must leave the operation in a consistent state.
                self._progress.set_phase("Verifying committed destination")
                destination_errors = self._verify_destination_snapshot(
                    destination, manifest, phase="Verifying committed destination"
                )
                if destination_errors:
                    failed_candidate = self._rollback_after_bad_destination(
                        destination, committed_backup
                    )
                    self._update_journal(
                        status="rolled_back_after_destination_verification_failure",
                        failed_candidate=str(failed_candidate) if failed_candidate else None,
                    )
                    raise VerificationError(
                        self._format_verification_errors([], destination_errors)
                    )

                self._progress.set_phase("Checking source before cleanup")
                post_commit_source_errors = self._verify_source_snapshot(
                    source, manifest, phase="Checking source before cleanup"
                )

                leftovers: list[Leftover]
                warnings: list[str] = []
                if post_commit_source_errors:
                    warning = (
                        "The source changed after the copy was committed. The source was "
                        "not deleted; the destination contains the verified snapshot."
                    )
                    warnings.append(warning)
                    self._log(warning)
                    leftovers = self._collect_all_source_leftovers(
                        source,
                        default_reason="cleanup skipped because source changed",
                        error_summary="; ".join(post_commit_source_errors[:20]),
                    )
                else:
                    self._progress.set_phase("Deleting verified source data")
                    leftovers = self._cleanup_source(source, manifest)

                db_logged = False
                fallback_incident_log: Path | None = None
                if source.exists():
                    self._log(
                        "Source folder remains after cleanup; collecting and recording all leftovers."
                    )
                    # Re-scan so lock files, newly-created files, and directories are all logged.
                    leftovers = self._merge_leftovers(
                        leftovers,
                        self._collect_all_source_leftovers(
                            source, default_reason="left after cleanup"
                        ),
                    )
                    db_logged, fallback_incident_log, db_warning = self._record_incident(
                        source=source,
                        destination=destination,
                        leftovers=leftovers,
                        error_summary=(
                            "; ".join(post_commit_source_errors[:20])
                            if post_commit_source_errors
                            else None
                        ),
                    )
                    if db_warning:
                        warnings.append(db_warning)
                        self._log(db_warning)

                if (
                    committed_backup
                    and not self.options.keep_destination_backup
                    and not source.exists()
                ):
                    self._log(f"Removing destination backup: {committed_backup}")
                    try:
                        shutil.rmtree(committed_backup)
                        committed_backup = None
                    except OSError as exc:
                        warning = f"Could not remove destination backup: {exc}"
                        warnings.append(warning)
                        self._log(warning)

                status = "completed" if not source.exists() else "completed_with_leftovers"
                self._update_journal(
                    status=status,
                    completed_at=self._utc_now(),
                    source_still_exists=source.exists(),
                    leftover_count=len(leftovers),
                    database_logged=db_logged,
                    fallback_incident_log=(
                        str(fallback_incident_log) if fallback_incident_log else None
                    ),
                )
                self._progress.finish(
                    "Completed" if status == "completed" else "Completed with leftovers"
                )
                if status == "completed":
                    self._log("Move completed. The source project folder was removed.")
                else:
                    self._log(
                        f"Move completed with {len(leftovers)} source leftover item(s)."
                    )

                return MoveResult(
                    run_id=self.run_id,
                    status=status,
                    source_project=str(source),
                    destination_project=str(destination),
                    backup_path=str(committed_backup) if committed_backup else None,
                    journal_path=str(self._journal_path),
                    renamed_logs=[str(destination / plan.log_rel) for plan in rename_plans],
                    leftovers=[asdict(item) for item in leftovers],
                    leftover_count=len(leftovers),
                    database_logged=db_logged,
                    fallback_incident_log=(
                        str(fallback_incident_log) if fallback_incident_log else None
                    ),
                    warnings=warnings,
                )

            except OperationCancelled:
                self._log("Cancellation accepted before commit; source and destination are unchanged.")
                self._safe_remove_staging(staging)
                self._update_journal(status="cancelled", completed_at=self._utc_now())
                raise
            except Exception as exc:
                if not self._committed:
                    self._safe_remove_staging(staging)
                self._update_journal(
                    status="failed",
                    completed_at=self._utc_now(),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

    # ------------------------------------------------------------------
    # Validation, scanning, and progress
    # ------------------------------------------------------------------

    def _validate_and_prepare_paths(self) -> tuple[Path, Path, Path]:
        source = Path(self.options.source_project).expanduser()
        destination_root = Path(self.options.destination_root).expanduser()

        if not source.exists() or not source.is_dir() or source.is_symlink():
            raise MoverError(f"Source project must be a real directory: {source}")
        source = source.resolve(strict=True)

        destination_root.mkdir(parents=True, exist_ok=True)
        if destination_root.is_symlink():
            destination_root = destination_root.resolve(strict=True)
        else:
            destination_root = destination_root.resolve(strict=True)

        destination = destination_root / source.name
        if destination.exists() and destination.is_symlink():
            raise MoverError(
                f"Destination project may not be a symbolic link: {destination}"
            )

        if self._paths_overlap(source, destination_root) or self._paths_overlap(
            source, destination
        ):
            raise MoverError(
                "Source and destination overlap. Choose separate Dir1 and Dir2 roots."
            )

        if not self.options.sensitive_markers:
            raise MoverError("At least one sensitive-folder marker is required.")
        if self.options.copy_retry_count < 1:
            raise MoverError("copy_retry_count must be at least 1.")
        if not self._valid_table_name(self.options.database_table):
            raise MoverError("database_table may contain only letters, digits, and underscores.")

        return source, destination_root, destination

    @staticmethod
    def _paths_overlap(a: Path, b: Path) -> bool:
        a_norm = os.path.normcase(os.path.abspath(a))
        b_norm = os.path.normcase(os.path.abspath(b))
        try:
            common = os.path.commonpath([a_norm, b_norm])
        except ValueError:
            return False  # Different Windows drives.
        return common == a_norm or common == b_norm

    @staticmethod
    def _valid_table_name(name: str) -> bool:
        return bool(name) and all(ch.isalnum() or ch == "_" for ch in name)

    def _scan_tree(self, root: Path, *, skip_lock_files: bool) -> list[ScanEntry]:
        entries: list[ScanEntry] = []

        def visit(directory: Path, rel_base: PurePosixPath) -> None:
            self._check_cancel()
            try:
                with os.scandir(directory) as iterator:
                    dir_entries = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError as exc:
                raise MoverError(f"Cannot scan directory {directory}: {exc}") from exc

            for dent in dir_entries:
                self._check_cancel()
                rel_path = rel_base / dent.name
                rel = rel_path.as_posix()
                try:
                    st = dent.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MoverError(f"Cannot stat {dent.path}: {exc}") from exc

                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    if skip_lock_files and self._is_lock_name(dent.name):
                        self._detail(f"LOCK FILE SKIPPED / PRESERVED: {rel}")
                        continue
                    try:
                        target = os.readlink(dent.path)
                    except OSError as exc:
                        raise MoverError(f"Cannot read symbolic link {dent.path}: {exc}") from exc
                    entries.append(self._scan_entry(rel, "symlink", st, target))
                elif stat.S_ISDIR(mode):
                    entries.append(self._scan_entry(rel, "directory", st))
                    visit(Path(dent.path), rel_path)
                elif stat.S_ISREG(mode):
                    if skip_lock_files and self._is_lock_name(dent.name):
                        self._detail(f"LOCK FILE SKIPPED / PRESERVED: {rel}")
                        continue
                    entries.append(self._scan_entry(rel, "file", st))
                else:
                    raise MoverError(
                        f"Unsupported special filesystem object: {dent.path}. "
                        "Only regular files, directories, and symbolic links are supported."
                    )

        visit(root, PurePosixPath())
        return entries

    @staticmethod
    def _scan_entry(
        rel: str, kind: Kind, st: os.stat_result, link_target: str | None = None
    ) -> ScanEntry:
        return ScanEntry(
            rel=rel,
            kind=kind,
            size=st.st_size if kind == "file" else 0,
            mtime_ns=st.st_mtime_ns,
            mode=stat.S_IMODE(st.st_mode),
            device=getattr(st, "st_dev", 0),
            inode=getattr(st, "st_ino", 0),
            link_target=link_target,
        )

    def _preflight_free_space(self, destination_root: Path, required: int) -> None:
        free = shutil.disk_usage(destination_root).free
        margin = max(256 * 1024 * 1024, int(required * 0.05))
        self._log(
            f"Free-space requirement: {self._human_size(required + margin)}; "
            f"available: {self._human_size(free)}."
        )
        if free < required + margin:
            raise MoverError(
                "Insufficient free space for the fail-safe staging copy. "
                f"Required approximately {self._human_size(required + margin)}, "
                f"available {self._human_size(free)}."
            )

    # ------------------------------------------------------------------
    # Copying
    # ------------------------------------------------------------------

    def _prepare_staging(self, staging: Path) -> None:
        if staging.exists() or staging.is_symlink():
            raise MoverError(f"Staging path already exists: {staging}")
        staging.mkdir(parents=False)
        self._fsync_directory(staging.parent)
        self._log(f"Created staging folder: {staging}")
        self._update_journal(status="staging_created")

    def _copy_tree_from_scan(
        self,
        source_root: Path,
        destination_root: Path,
        scan: Sequence[ScanEntry],
        *,
        phase: str,
        collect_manifest: bool,
    ) -> dict[str, ManifestRecord]:
        manifest: dict[str, ManifestRecord] = {}
        directories = sorted(
            (e for e in scan if e.kind == "directory"),
            key=lambda e: (len(PurePosixPath(e.rel).parts), e.rel.casefold()),
        )
        non_directories = sorted(
            (e for e in scan if e.kind != "directory"), key=lambda e: e.rel.casefold()
        )

        for entry in directories:
            self._check_cancel()
            src = self._local_path(source_root, entry.rel)
            dst = self._local_path(destination_root, entry.rel)
            self._operation(f"{phase}: PREPARE FOLDER {entry.rel}")
            self._ensure_real_directory(dst)
            if collect_manifest:
                current = os.lstat(src)
                manifest[entry.rel] = self._manifest_from_stat(
                    entry.rel, entry.rel, "directory", current
                )

        for entry in non_directories:
            self._check_cancel()
            src = self._local_path(source_root, entry.rel)
            dst = self._local_path(destination_root, entry.rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            destination_exists = dst.exists() or dst.is_symlink()
            if entry.kind == "file":
                action = "REPLACE FILE" if destination_exists else "COPY FILE"
                self._operation(
                    f"{phase}: {action} {entry.rel} ({self._human_size(entry.size)})"
                )
                self._replace_directory_if_needed(dst)
                digest, st = self._copy_regular_file_atomic(src, dst, phase)
                if collect_manifest:
                    manifest[entry.rel] = self._manifest_from_stat(
                        entry.rel,
                        entry.rel,
                        "file",
                        st,
                        sha256=digest,
                    )
            elif entry.kind == "symlink":
                action = "REPLACE SYMLINK" if destination_exists else "COPY SYMLINK"
                self._operation(f"{phase}: {action} {entry.rel}")
                self._replace_any_path(dst)
                target, st = self._copy_symlink_atomic(src, dst)
                if collect_manifest:
                    manifest[entry.rel] = self._manifest_from_stat(
                        entry.rel,
                        entry.rel,
                        "symlink",
                        st,
                        link_target=target,
                    )

        # Apply directory metadata after children so child creation does not alter it.
        for entry in sorted(
            directories,
            key=lambda e: len(PurePosixPath(e.rel).parts),
            reverse=True,
        ):
            src = self._local_path(source_root, entry.rel)
            dst = self._local_path(destination_root, entry.rel)
            self._copy_metadata(src, dst, follow_symlinks=False)

        return manifest

    def _copy_regular_file_atomic(
        self, source: Path, destination: Path, phase: str
    ) -> tuple[str, os.stat_result]:
        last_error: Exception | None = None
        for attempt in range(1, self.options.copy_retry_count + 1):
            self._check_cancel()
            temp = destination.parent / (
                f".{destination.name}.partial.{secrets.token_hex(8)}"
            )
            try:
                before = os.stat(source, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    raise SourceChangedError(f"Source is no longer a regular file: {source}")

                digest = hashlib.sha256()
                with open(source, "rb", buffering=0) as src, open(temp, "xb", buffering=0) as dst:
                    while True:
                        self._check_cancel()
                        chunk = src.read(self.COPY_BUFFER_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                        digest.update(chunk)
                        if self._progress:
                            self._progress.add(len(chunk))
                    dst.flush()
                    if self.options.durable_writes:
                        os.fsync(dst.fileno())

                after = os.stat(source, follow_symlinks=False)
                if not self._same_file_fingerprint(before, after):
                    raise SourceChangedError(f"File changed while being copied: {source}")

                self._copy_metadata(source, temp, follow_symlinks=False)
                if self.options.durable_writes:
                    with open(temp, "rb", buffering=0) as durable:
                        os.fsync(durable.fileno())

                os.replace(temp, destination)
                self._fsync_directory(destination.parent)
                return digest.hexdigest(), after
            except Exception as exc:
                last_error = exc
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                if isinstance(exc, OperationCancelled):
                    raise
                if attempt < self.options.copy_retry_count:
                    self._log(
                        f"Retrying copy ({attempt}/{self.options.copy_retry_count}) for "
                        f"{source}: {exc}"
                    )
                    time.sleep(min(0.25 * attempt, 1.0))
                    continue
                break

        raise MoverError(f"Could not copy {source}: {last_error}") from last_error

    def _copy_symlink_atomic(
        self, source: Path, destination: Path
    ) -> tuple[str, os.stat_result]:
        before = os.lstat(source)
        target_before = os.readlink(source)
        temp = destination.parent / f".{destination.name}.partial.{secrets.token_hex(8)}"
        try:
            target_is_directory = os.path.isdir(source)
            os.symlink(target_before, temp, target_is_directory=target_is_directory)
            after = os.lstat(source)
            target_after = os.readlink(source)
            if not self._same_file_fingerprint(before, after) or target_before != target_after:
                raise SourceChangedError(f"Symbolic link changed while being copied: {source}")
            self._copy_metadata(source, temp, follow_symlinks=False)
            os.replace(temp, destination)
            self._fsync_directory(destination.parent)
            return target_after, after
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _copy_root_metadata(self, source_root: Path, destination_root: Path) -> None:
        self._copy_metadata(source_root, destination_root, follow_symlinks=False)

    @staticmethod
    def _copy_metadata(source: Path, destination: Path, *, follow_symlinks: bool) -> None:
        try:
            shutil.copystat(source, destination, follow_symlinks=follow_symlinks)
        except OSError:
            # Content verification is authoritative. Some platforms/filesystems cannot
            # apply every metadata field, especially to symbolic links.
            pass

    def _ensure_real_directory(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                return
            self._remove_staging_path(path)
        path.mkdir(parents=True, exist_ok=True)

    def _replace_directory_if_needed(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                self._remove_staging_path(path)

    def _replace_any_path(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            self._remove_staging_path(path)

    @staticmethod
    def _remove_staging_path(path: Path) -> None:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    # ------------------------------------------------------------------
    # Sensitive-folder randomization
    # ------------------------------------------------------------------

    def _randomize_sensitive_folders(self, staging: Path) -> list[RenamePlan]:
        scan = self._scan_tree(staging, skip_lock_files=False)
        candidates = [
            e.rel
            for e in scan
            if e.kind == "directory" and self._is_sensitive_directory_name(PurePosixPath(e.rel).name)
        ]
        candidates.sort(key=lambda rel: (len(PurePosixPath(rel).parts), rel.casefold()))

        # A top-most match is processed once. This avoids ambiguous double-randomization
        # when one TRASH/OTHER folder is nested inside another.
        roots: list[str] = []
        for candidate in candidates:
            candidate_path = PurePosixPath(candidate)
            if any(self._pure_is_relative_to(candidate_path, PurePosixPath(root)) for root in roots):
                continue
            roots.append(candidate)

        if not roots:
            self._log("No folder name matched the configured TRASH/OTHER markers.")

        plans: list[RenamePlan] = []
        for root_rel in roots:
            self._check_cancel()
            root = self._local_path(staging, root_rel)
            plan = self._randomize_one_folder(staging, root, root_rel)
            plans.append(plan)
            self._log(
                f"Randomized {plan.renamed_count} item(s) in {root_rel}; "
                f"log: {plan.log_rel}"
            )
        return plans

    def _randomize_one_folder(
        self, staging: Path, root: Path, root_rel: str
    ) -> RenamePlan:
        entries = self._scan_tree(root, skip_lock_files=False)
        if not entries:
            log_rel = (PurePosixPath(root_rel) / f"_rename_log_{self.run_id}.jsonl").as_posix()
            self._write_jsonl(self._local_path(staging, log_rel), [])
            return RenamePlan(root_rel, log_rel, {}, 0)

        used_by_parent: dict[str, set[str]] = {}
        for entry in entries:
            parent = PurePosixPath(entry.rel).parent.as_posix()
            used_by_parent.setdefault(parent, set()).add(
                self._normalize_name(PurePosixPath(entry.rel).name)
            )

        component_names: dict[str, str] = {}
        for entry in sorted(entries, key=lambda e: e.rel.casefold()):
            rel_path = PurePosixPath(entry.rel)
            parent_key = rel_path.parent.as_posix()
            used = used_by_parent.setdefault(parent_key, set())
            suffix = ""
            if (
                self.options.preserve_file_extension_when_randomizing
                and entry.kind != "directory"
            ):
                candidate_suffix = Path(rel_path.name).suffix
                if 0 < len(candidate_suffix) <= 20:
                    suffix = candidate_suffix
            while True:
                random_name = f"{secrets.token_hex(16)}{suffix}"
                normalized = self._normalize_name(random_name)
                if normalized not in used:
                    used.add(normalized)
                    component_names[entry.rel] = random_name
                    break

        mapping: dict[str, str] = {}
        log_rows: list[dict] = []
        by_rel = {entry.rel: entry for entry in entries}
        for entry in entries:
            original_parts = PurePosixPath(entry.rel).parts
            transformed_parts: list[str] = []
            for index in range(len(original_parts)):
                cumulative = PurePosixPath(*original_parts[: index + 1]).as_posix()
                transformed_parts.append(component_names[cumulative])
            original_full = (PurePosixPath(root_rel) / entry.rel).as_posix()
            final_full = (
                PurePosixPath(root_rel) / PurePosixPath(*transformed_parts)
            ).as_posix()
            mapping[original_full] = final_full
            log_rows.append(
                {
                    "run_id": self.run_id,
                    "timestamp_utc": self._utc_now(),
                    "original_relative_path": original_full,
                    "renamed_relative_path": final_full,
                    "kind": entry.kind,
                    "size": entry.size,
                }
            )

        # Descending depth keeps every parent at its original path until its turn.
        for rel in sorted(
            by_rel,
            key=lambda value: (len(PurePosixPath(value).parts), value.casefold()),
            reverse=True,
        ):
            self._check_cancel()
            original_full = (PurePosixPath(root_rel) / rel).as_posix()
            final_full = mapping[original_full]
            self._operation(f"RANDOMIZE NAME: {original_full} -> {final_full}")
            current = self._local_path(root, rel)
            target = current.with_name(component_names[rel])
            if target.exists() or target.is_symlink():
                raise MoverError(f"Unexpected random-name collision: {target}")
            os.replace(current, target)
            self._fsync_directory(target.parent)

        log_rel = (PurePosixPath(root_rel) / f"_rename_log_{self.run_id}.jsonl").as_posix()
        self._operation(f"WRITE RENAME LOG: {log_rel}")
        self._write_jsonl(self._local_path(staging, log_rel), log_rows)
        return RenamePlan(root_rel, log_rel, mapping, len(entries))

    @staticmethod
    def _pure_is_relative_to(path: PurePosixPath, parent: PurePosixPath) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _apply_rename_mapping_to_manifest(
        self,
        manifest: dict[str, ManifestRecord],
        plans: Sequence[RenamePlan],
    ) -> None:
        combined: dict[str, str] = {}
        for plan in plans:
            combined.update(plan.mapping)
        for record in manifest.values():
            record.final_rel = combined.get(record.rel, record.rel)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def _verify_source_snapshot(
        self,
        source: Path,
        manifest: dict[str, ManifestRecord],
        *,
        phase: str,
    ) -> list[str]:
        errors: list[str] = []
        if not source.exists():
            return [f"Source project disappeared: {source}"]

        try:
            current_scan = self._scan_tree(source, skip_lock_files=True)
        except Exception as exc:
            return [str(exc)]

        current_by_rel = {entry.rel: entry for entry in current_scan}
        expected_rels = set(manifest)
        current_rels = set(current_by_rel)

        for rel in sorted(expected_rels - current_rels):
            errors.append(f"Missing source item: {rel}")
        for rel in sorted(current_rels - expected_rels):
            errors.append(f"New source item not copied: {rel}")

        for rel in sorted(expected_rels & current_rels):
            self._check_cancel()
            self._operation(f"{phase}: VERIFY SOURCE {rel}")
            expected = manifest[rel]
            current = current_by_rel[rel]
            if current.kind != expected.kind:
                errors.append(
                    f"Source type changed for {rel}: expected {expected.kind}, got {current.kind}"
                )
                continue
            path = self._local_path(source, rel)
            try:
                if expected.kind == "file":
                    digest, st = self._hash_file_stable(path, phase)
                    if digest != expected.sha256:
                        errors.append(f"Source content changed: {rel}")
                    if not self._record_matches_stat(expected, st):
                        errors.append(f"Source metadata/identity changed: {rel}")
                elif expected.kind == "symlink":
                    st = os.lstat(path)
                    target = os.readlink(path)
                    if target != expected.link_target:
                        errors.append(f"Source symlink target changed: {rel}")
                    if not self._record_matches_stat(expected, st):
                        errors.append(f"Source symlink identity changed: {rel}")
                else:
                    st = os.lstat(path)
                    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                        errors.append(f"Source directory changed type: {rel}")
            except OSError as exc:
                errors.append(f"Cannot verify source {rel}: {exc}")
        return self._deduplicate(errors)

    def _verify_destination_snapshot(
        self,
        destination: Path,
        manifest: dict[str, ManifestRecord],
        *,
        phase: str,
    ) -> list[str]:
        errors: list[str] = []
        for record in sorted(manifest.values(), key=lambda item: item.final_rel.casefold()):
            self._check_cancel()
            self._operation(f"{phase}: VERIFY DESTINATION {record.final_rel}")
            path = self._local_path(destination, record.final_rel)
            try:
                st = os.lstat(path)
            except OSError as exc:
                errors.append(f"Missing destination item {record.final_rel}: {exc}")
                continue

            if record.kind == "file":
                if not stat.S_ISREG(st.st_mode):
                    errors.append(f"Destination is not a regular file: {record.final_rel}")
                    continue
                try:
                    digest = self._hash_file(path, phase)
                except OSError as exc:
                    errors.append(f"Cannot hash destination {record.final_rel}: {exc}")
                    continue
                if digest != record.sha256:
                    errors.append(f"Destination hash mismatch: {record.final_rel}")
            elif record.kind == "symlink":
                if not stat.S_ISLNK(st.st_mode):
                    errors.append(f"Destination is not a symlink: {record.final_rel}")
                    continue
                try:
                    if os.readlink(path) != record.link_target:
                        errors.append(
                            f"Destination symlink target mismatch: {record.final_rel}"
                        )
                except OSError as exc:
                    errors.append(f"Cannot read destination symlink {record.final_rel}: {exc}")
            else:
                if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    errors.append(f"Destination is not a real directory: {record.final_rel}")
        return self._deduplicate(errors)

    def _hash_file_stable(self, path: Path, phase: str) -> tuple[str, os.stat_result]:
        before = os.stat(path, follow_symlinks=False)
        digest = self._hash_file(path, phase)
        after = os.stat(path, follow_symlinks=False)
        if not self._same_file_fingerprint(before, after):
            raise SourceChangedError(f"File changed while being verified: {path}")
        return digest, after

    def _hash_file(self, path: Path, phase: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb", buffering=0) as handle:
            while True:
                self._check_cancel()
                chunk = handle.read(self.COPY_BUFFER_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                if self._progress:
                    self._progress.add(len(chunk))
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # Commit and rollback
    # ------------------------------------------------------------------

    def _commit(self, staging: Path, destination: Path, backup: Path) -> Path | None:
        destination_existed = destination.exists()
        moved_to_backup = False
        try:
            if destination_existed:
                if backup.exists() or backup.is_symlink():
                    raise MoverError(f"Backup path unexpectedly exists: {backup}")
                os.replace(destination, backup)
                moved_to_backup = True
                self._fsync_directory(destination.parent)
                self._log(f"Previous destination moved to backup: {backup}")
            os.replace(staging, destination)
            self._fsync_directory(destination.parent)
            self._log(f"Staged project committed to: {destination}")
            return backup if destination_existed else None
        except Exception:
            if moved_to_backup and not destination.exists() and backup.exists():
                try:
                    os.replace(backup, destination)
                    self._fsync_directory(destination.parent)
                    self._log("Commit failed; previous destination was restored.")
                except OSError as rollback_exc:
                    self._log(
                        "CRITICAL: commit failed and automatic destination restore also failed: "
                        f"{rollback_exc}. Backup remains at {backup}."
                    )
            raise

    def _rollback_after_bad_destination(
        self, destination: Path, backup: Path | None
    ) -> Path | None:
        failed = destination.parent / (
            f".{destination.name}.failed_verification.{self.run_id.replace('-', '')[:12]}"
        )
        if failed.exists() or failed.is_symlink():
            failed = destination.parent / (
                f".{destination.name}.failed_verification.{secrets.token_hex(8)}"
            )
        os.replace(destination, failed)
        if backup and backup.exists():
            os.replace(backup, destination)
            self._fsync_directory(destination.parent)
            self._log(
                f"Destination verification failed. Previous destination restored; "
                f"failed candidate preserved at {failed}."
            )
        else:
            self._fsync_directory(destination.parent)
            self._log(
                f"Destination verification failed. Failed candidate preserved at {failed}; "
                "source was not deleted."
            )
        return failed

    # ------------------------------------------------------------------
    # Source cleanup and incident logging
    # ------------------------------------------------------------------

    def _cleanup_source(
        self, source: Path, manifest: dict[str, ManifestRecord]
    ) -> list[Leftover]:
        leftovers: list[Leftover] = []
        records = sorted(
            manifest.values(),
            key=lambda item: (len(PurePosixPath(item.rel).parts), item.rel.casefold()),
            reverse=True,
        )

        # Files and links first. The post-commit hash pass just completed; this
        # final identity check prevents deleting a normally-detectable replacement.
        for record in records:
            if record.kind == "directory":
                continue
            path = self._local_path(source, record.rel)
            if not (path.exists() or path.is_symlink()):
                continue
            self._operation(f"DELETE SOURCE {record.kind.upper()}: {record.rel}")
            try:
                st = os.lstat(path)
                if not self._record_matches_stat(record, st):
                    reason = (
                        "not deleted because identity/metadata changed after verification"
                    )
                    leftovers.append(Leftover(record.rel, record.kind, reason))
                    self._detail(f"SOURCE ITEM PRESERVED: {record.rel} ({reason})")
                    continue
                if record.kind == "symlink" and os.readlink(path) != record.link_target:
                    reason = (
                        "not deleted because symlink target changed after verification"
                    )
                    leftovers.append(Leftover(record.rel, record.kind, reason))
                    self._detail(f"SOURCE ITEM PRESERVED: {record.rel} ({reason})")
                    continue
                path.unlink()
            except OSError as exc:
                leftovers.append(
                    Leftover(record.rel, record.kind, "delete failed", str(exc))
                )
                self._detail(f"DELETE FAILED: {record.rel}: {exc}")

        # Remove directories only when empty. Lock files and failed deletions remain.
        for record in records:
            if record.kind != "directory":
                continue
            path = self._local_path(source, record.rel)
            if not path.exists():
                continue
            self._operation(f"REMOVE EMPTY SOURCE FOLDER: {record.rel}")
            try:
                path.rmdir()
            except OSError:
                self._detail(f"SOURCE FOLDER PRESERVED (not empty or locked): {record.rel}")

        if source.exists():
            self._operation(f"REMOVE SOURCE PROJECT FOLDER: {source}")
            try:
                source.rmdir()
            except OSError:
                self._detail(f"SOURCE PROJECT FOLDER PRESERVED: {source}")

        return leftovers

    def _collect_all_source_leftovers(
        self,
        source: Path,
        *,
        default_reason: str,
        error_summary: str | None = None,
    ) -> list[Leftover]:
        if not source.exists():
            return []
        try:
            scan = self._scan_tree(source, skip_lock_files=False)
        except Exception as exc:
            return [Leftover(".", "directory", default_reason, str(exc))]

        leftovers: list[Leftover] = []
        for entry in scan:
            name = PurePosixPath(entry.rel).name
            if entry.kind != "directory" and self._is_lock_name(name):
                reason = "lock file intentionally preserved"
            else:
                reason = default_reason
            leftovers.append(
                Leftover(entry.rel, entry.kind, reason, error_summary if error_summary else None)
            )
            self._detail(f"LEFTOVER: {entry.rel} ({entry.kind}) - {reason}")
        if not scan:
            leftovers.append(Leftover(".", "directory", default_reason, error_summary))
        return leftovers

    @staticmethod
    def _merge_leftovers(
        first: Sequence[Leftover], second: Sequence[Leftover]
    ) -> list[Leftover]:
        merged: dict[tuple[str, str], Leftover] = {}
        for item in [*first, *second]:
            key = (item.relative_path, item.kind)
            if key not in merged:
                merged[key] = item
            elif merged[key].error is None and item.error:
                merged[key] = item
        return sorted(merged.values(), key=lambda item: item.relative_path.casefold())

    def _record_incident(
        self,
        *,
        source: Path,
        destination: Path,
        leftovers: Sequence[Leftover],
        error_summary: str | None,
    ) -> tuple[bool, Path | None, str | None]:
        self._log(f"Recording {len(leftovers)} source leftover item(s).")
        payload = {
            "run_id": self.run_id,
            "folder_name": source.name,
            "source_path": str(source),
            "destination_path": str(destination),
            "leftover_count": len(leftovers),
            "leftovers": [
                asdict(item)
                for item in leftovers[: self.options.max_leftovers_in_database]
            ],
            "leftovers_truncated": len(leftovers)
            > self.options.max_leftovers_in_database,
            "error_summary": error_summary,
            "created_at": self._utc_now(),
        }

        dsn = (self.options.postgres_dsn or "").strip()
        if dsn:
            try:
                import psycopg
                from psycopg.types.json import Jsonb

                table = self.options.database_table
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id BIGSERIAL PRIMARY KEY,
                        run_id UUID NOT NULL UNIQUE,
                        folder_name TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        destination_path TEXT NOT NULL,
                        leftover_count INTEGER NOT NULL,
                        leftovers JSONB NOT NULL,
                        error_summary TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """
                insert_sql = f"""
                    INSERT INTO {table} (
                        run_id, folder_name, source_path, destination_path,
                        leftover_count, leftovers, error_summary, created_at
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (run_id) DO UPDATE SET
                        folder_name = EXCLUDED.folder_name,
                        source_path = EXCLUDED.source_path,
                        destination_path = EXCLUDED.destination_path,
                        leftover_count = EXCLUDED.leftover_count,
                        leftovers = EXCLUDED.leftovers,
                        error_summary = EXCLUDED.error_summary
                """
                with psycopg.connect(dsn) as connection:
                    connection.execute(create_sql)
                    connection.execute(
                        insert_sql,
                        (
                            self.run_id,
                            source.name,
                            str(source),
                            str(destination),
                            len(leftovers),
                            Jsonb(payload),
                            error_summary,
                        ),
                    )
                self._log("Incomplete-cleanup record written to PostgreSQL.")
                return True, None, None
            except Exception as exc:
                fallback = self._write_incident_fallback(destination, payload)
                warning = (
                    f"PostgreSQL incident logging failed ({exc}). "
                    f"Fallback JSON was written to {fallback}."
                )
                return False, fallback, warning

        fallback = self._write_incident_fallback(destination, payload)
        warning = (
            "No PostgreSQL DSN was supplied. Incomplete-cleanup details were written "
            f"to {fallback}."
        )
        return False, fallback, warning

    def _write_incident_fallback(self, destination: Path, payload: dict) -> Path:
        fallback = destination / f"_incomplete_cleanup_{self.run_id}.json"
        self._atomic_write_json(fallback, payload)
        return fallback

    # ------------------------------------------------------------------
    # Audit and journal
    # ------------------------------------------------------------------

    def _write_audit_manifest(
        self,
        staging: Path,
        source: Path,
        destination: Path,
        manifest: dict[str, ManifestRecord],
        rename_plans: Sequence[RenamePlan],
    ) -> Path:
        path = staging / f"_move_manifest_{self.run_id}.json"
        payload = {
            "run_id": self.run_id,
            "created_at": self._utc_now(),
            "source": str(source),
            "destination": str(destination),
            "hash_algorithm": "SHA-256",
            "lock_patterns_not_copied_or_deleted": list(self.options.lock_patterns),
            "sensitive_markers": list(self.options.sensitive_markers),
            "rename_logs": [plan.log_rel for plan in rename_plans],
            "source_snapshot": [
                asdict(record)
                for record in sorted(manifest.values(), key=lambda item: item.rel.casefold())
            ],
        }
        self._atomic_write_json(path, payload)
        self._log(f"Audit manifest written: {path}")
        return path

    def _write_journal(self) -> None:
        if self._journal_path is None:
            return
        self._atomic_write_json(self._journal_path, self._journal)

    def _update_journal(self, **updates) -> None:
        if not self._journal:
            return
        self._journal.update(updates)
        self._journal["updated_at"] = self._utc_now()
        try:
            self._write_journal()
        except OSError as exc:
            self._log(f"Warning: could not update operation journal: {exc}")

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set() and not self._committed:
            raise OperationCancelled("Operation cancelled before commit.")

    def _is_lock_name(self, name: str) -> bool:
        normalized = name.casefold()
        return any(
            fnmatch.fnmatchcase(normalized, pattern.casefold())
            for pattern in self.options.lock_patterns
        )

    def _is_sensitive_directory_name(self, name: str) -> bool:
        normalized = name.casefold()
        return any(marker.casefold() in normalized for marker in self.options.sensitive_markers)

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.casefold() if os.name == "nt" else name

    @staticmethod
    def _local_path(root: Path, rel: str) -> Path:
        if not rel:
            return root
        return root.joinpath(*PurePosixPath(rel).parts)

    @staticmethod
    def _manifest_from_stat(
        rel: str,
        final_rel: str,
        kind: Kind,
        st: os.stat_result,
        *,
        sha256: str | None = None,
        link_target: str | None = None,
    ) -> ManifestRecord:
        return ManifestRecord(
            rel=rel,
            final_rel=final_rel,
            kind=kind,
            size=st.st_size if kind == "file" else 0,
            mtime_ns=st.st_mtime_ns,
            mode=stat.S_IMODE(st.st_mode),
            device=getattr(st, "st_dev", 0),
            inode=getattr(st, "st_ino", 0),
            sha256=sha256,
            link_target=link_target,
        )

    @staticmethod
    def _same_file_fingerprint(a: os.stat_result, b: os.stat_result) -> bool:
        return (
            a.st_size == b.st_size
            and a.st_mtime_ns == b.st_mtime_ns
            and stat.S_IMODE(a.st_mode) == stat.S_IMODE(b.st_mode)
            and getattr(a, "st_dev", 0) == getattr(b, "st_dev", 0)
            and getattr(a, "st_ino", 0) == getattr(b, "st_ino", 0)
        )

    @staticmethod
    def _record_matches_stat(record: ManifestRecord, st: os.stat_result) -> bool:
        return (
            record.size == (st.st_size if record.kind == "file" else 0)
            and record.mtime_ns == st.st_mtime_ns
            and record.mode == stat.S_IMODE(st.st_mode)
            and record.device == getattr(st, "st_dev", 0)
            and record.inode == getattr(st, "st_ino", 0)
        )

    def _fsync_directory(self, directory: Path) -> None:
        if not self.options.durable_writes or os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.partial.{secrets.token_hex(8)}"
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                if self.options.durable_writes:
                    os.fsync(handle.fileno())
            os.replace(temp, path)
            self._fsync_directory(path.parent)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _write_jsonl(self, path: Path, rows: Iterable[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.partial.{secrets.token_hex(8)}"
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                    handle.write("\n")
                handle.flush()
                if self.options.durable_writes:
                    os.fsync(handle.fileno())
            os.replace(temp, path)
            self._fsync_directory(path.parent)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _safe_remove_staging(self, staging: Path) -> None:
        if not (staging.exists() or staging.is_symlink()):
            return
        try:
            if staging.is_symlink():
                staging.unlink()
            else:
                shutil.rmtree(staging)
            self._log(f"Removed uncommitted staging directory: {staging}")
        except OSError as exc:
            self._log(
                f"Could not remove uncommitted staging directory {staging}: {exc}. "
                "The source and live destination were not deleted."
            )

    def _operation(self, message: str) -> None:
        if not self.options.log_each_item:
            return
        if self._progress:
            self._progress.set_phase(message)
        self._log(message)

    def _detail(self, message: str) -> None:
        if self.options.log_each_item:
            self._log(message)

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _human_size(value: int) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    @staticmethod
    def _deduplicate(items: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(items))

    @staticmethod
    def _format_verification_errors(
        source_errors: Sequence[str], destination_errors: Sequence[str]
    ) -> str:
        combined = [
            *(f"SOURCE: {item}" for item in source_errors),
            *(f"DESTINATION: {item}" for item in destination_errors),
        ]
        preview = combined[:30]
        suffix = "" if len(combined) <= 30 else f"; ... {len(combined) - 30} more"
        return "Verification failed: " + "; ".join(preview) + suffix
