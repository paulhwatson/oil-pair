import os

import pytest

from oil_pair import instance_lock


def test_acquire_creates_lock_file_with_own_pid(tmp_path):
    path = tmp_path / "instance.lock"

    instance_lock.acquire(path)

    assert path.read_text().strip() == str(os.getpid())


def test_release_removes_lock_file_owned_by_self(tmp_path):
    path = tmp_path / "instance.lock"
    instance_lock.acquire(path)

    instance_lock.release(path)

    assert not path.exists()


def test_acquire_raises_when_a_live_process_holds_the_lock(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    path.write_text("99999")  # arbitrary - liveness is mocked below
    monkeypatch.setattr(instance_lock, "_pid_is_alive", lambda pid: True)

    with pytest.raises(instance_lock.AlreadyRunningError):
        instance_lock.acquire(path)


def test_acquire_reclaims_a_stale_lock_from_a_dead_process(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    path.write_text("99999")
    monkeypatch.setattr(instance_lock, "_pid_is_alive", lambda pid: False)

    instance_lock.acquire(path)  # must not raise

    assert path.read_text().strip() == str(os.getpid())


def test_release_does_not_remove_lock_file_owned_by_a_different_pid(tmp_path):
    path = tmp_path / "instance.lock"
    path.write_text("99999")

    instance_lock.release(path)

    assert path.exists()
