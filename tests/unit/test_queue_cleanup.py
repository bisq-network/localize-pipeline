"""Queue cleanup must work without write access to the queue's parent."""

import os

import pytest

from localize.core.connectors import FilesystemSourceConnector
from localize.queue_cleanup import clear_queue_contents
from localize.translate_localization_files import cleanup_translation_queue_folders


@pytest.fixture(params=["runtime", "filesystem_connector"])
def cleanup(request, tmp_path):
    if request.param == "runtime":
        return cleanup_translation_queue_folders
    connector = FilesystemSourceConnector(detect_changed_translation_files=lambda *a, **kw: [])
    repo = tmp_path / "repo"
    input_folder = repo / "i18n"
    input_folder.mkdir(parents=True)

    def cleanup_validated(queue, translated):
        connector.validate_paths(str(input_folder), queue, translated, str(repo))
        connector.cleanup_queue_folders(queue, translated)

    return cleanup_validated


def make_queues(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    queues = [app / "translation_queue", app / "translated_queue"]
    for queue in queues:
        (queue / "nested").mkdir(parents=True)
        (queue / "nested" / "stale.properties").write_text("stale=value\n")
        (queue / ".hidden").write_text("stale")
    return app, queues


@pytest.mark.skipif(os.geteuid() == 0, reason="Requires real non-root permission enforcement")
def test_cleanup_retains_roots_under_unwritable_parent(cleanup, tmp_path):
    app, queues = make_queues(tmp_path)
    root_inodes = [queue.stat().st_ino for queue in queues]
    probe = app / "probe"
    probe.mkdir()
    app.chmod(0o555)
    try:
        # Verify this filesystem enforces the exact permission behind the outage.
        with pytest.raises(PermissionError):
            probe.rmdir()
        cleanup(*(str(queue) for queue in queues))
        assert [queue.stat().st_ino for queue in queues] == root_inodes
        assert all(list(queue.iterdir()) == [] for queue in queues)
    finally:
        app.chmod(0o755)


def test_cleanup_unlinks_symlinks_without_touching_targets(cleanup, tmp_path):
    _, queues = make_queues(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected_file = outside / "pending.properties"
    protected_file.write_text("keep=value\n")
    (queues[0] / "linked-directory").symlink_to(outside, target_is_directory=True)
    (queues[1] / "linked-file").symlink_to(protected_file)
    cleanup(*(str(queue) for queue in queues))
    assert protected_file.read_text() == "keep=value\n"
    assert all(list(queue.iterdir()) == [] for queue in queues)


@pytest.mark.skipif(os.geteuid() == 0, reason="Requires real non-root permission enforcement")
def test_cleanup_propagates_content_permission_errors(cleanup, tmp_path):
    _, queues = make_queues(tmp_path)
    blocked = queues[0] / "nested"
    blocked.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            cleanup(*(str(queue) for queue in queues))
        assert (blocked / "stale.properties").exists()
        assert (queues[1] / "nested" / "stale.properties").exists()
    finally:
        blocked.chmod(0o755)


def test_missing_queue_is_already_clear(tmp_path):
    clear_queue_contents(str(tmp_path / "missing"))


def test_queue_file_is_not_silently_ignored(tmp_path):
    queue = tmp_path / "queue"
    queue.write_text("preserve")
    with pytest.raises(NotADirectoryError):
        clear_queue_contents(str(queue))
    assert queue.read_text() == "preserve"
