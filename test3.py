def on_correction(self) -> None:
    source_path = Path(self.input_path)

    correction_path = self.save_correction(show_message=False)
    if correction_path is None:
        return

    # Release the source PDF before deleting it on Windows.
    self._close_current_document()

    deleted_source = ""
    try:
        if source_path.exists():
            source_path.unlink()
            deleted_source = str(source_path)
    except OSError as exc:
        QMessageBox.warning(
            self,
            "Source deletion failed",
            f"Correction was saved, but the source PDF could not be deleted:\n{exc}",
        )

    # Trigger the function connected in main.py.
    self.correction_completed.emit(
        str(correction_path),
        deleted_source,
    )

    # Close the PDF editor after the main callback has run.
    QTimer.singleShot(0, self.close)

self.correction_completed.emit(str(correction_path), deleted_source)
QTimer.singleShot(0, self.close)

self.load_pdf(correction_path)
# or
self.reopen_pdf(correction_path)
