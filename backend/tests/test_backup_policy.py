"""run_backup: one dump at a time, a recent complete dump is reused, and an
interrupted pg_dump never leaves a file that looks like a valid backup.

pg_dump and the advisory lock are replaced by fakes - no database and no
real dump are involved."""

import contextlib
import os
import subprocess
import time

import pytest

from app.core import backup
from app.core.config import get_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_reuse_minutes", 360)
    monkeypatch.setattr(settings, "backup_timeout_seconds", 900)
    monkeypatch.setattr(backup, "_backup_lock", contextlib.nullcontext)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        target = args[args.index("-f") + 1]
        with open(target, "wb") as fh:
            fh.write(b"PGDMP fake dump")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return tmp_path, calls


def _make_dump(directory, name, age_minutes, content=b"PGDMP"):
    path = directory / name
    path.write_bytes(content)
    old = time.time() - age_minutes * 60
    os.utime(path, (old, old))
    return path


def test_dumps_when_no_backup_exists(env):
    directory, calls = env
    written = backup.run_backup("pre_migration")
    assert len(calls) == 1
    assert written.exists() and written.suffix == ".dump"
    assert "-f" in calls[0] and calls[0][calls[0].index("-f") + 1].endswith(".dump.partial")
    assert not list(directory.glob("*.partial"))


def test_recent_complete_dump_is_reused(env):
    directory, calls = env
    recent = _make_dump(directory, "orbitia_pre_migration_20261008-100000.dump", age_minutes=30)
    assert backup.run_backup("pre_migration") == recent
    assert calls == []


def test_old_dump_triggers_a_new_one(env):
    directory, calls = env
    _make_dump(directory, "orbitia_pre_migration_20261001-100000.dump", age_minutes=600)
    written = backup.run_backup("pre_migration")
    assert len(calls) == 1
    assert written.name != "orbitia_pre_migration_20261001-100000.dump"


def test_zero_window_always_dumps(env):
    directory, calls = env
    _make_dump(directory, "orbitia_daily_20261008-100000.dump", age_minutes=1)
    backup.run_backup("manual", reuse_minutes=0)
    assert len(calls) == 1


def test_empty_or_partial_files_are_never_reused(env):
    directory, calls = env
    _make_dump(directory, "orbitia_pre_migration_20261008-110000.dump", age_minutes=1, content=b"")
    _make_dump(directory, "orbitia_pre_migration_20261008-120000.dump.partial", age_minutes=1)
    backup.run_backup("pre_migration")
    assert len(calls) == 1


def test_failed_dump_leaves_no_file(env, monkeypatch):
    directory, _ = env

    def failing_run(args, **kwargs):
        target = args[args.index("-f") + 1]
        with open(target, "wb") as fh:
            fh.write(b"half")
        return subprocess.CompletedProcess(args, 1, "", "boom")

    monkeypatch.setattr(backup.subprocess, "run", failing_run)
    with pytest.raises(RuntimeError, match="exited 1"):
        backup.run_backup("pre_migration")
    assert list(directory.iterdir()) == []


def test_timed_out_dump_leaves_no_file(env, monkeypatch):
    directory, _ = env

    def slow_run(args, **kwargs):
        target = args[args.index("-f") + 1]
        with open(target, "wb") as fh:
            fh.write(b"half")
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(backup.subprocess, "run", slow_run)
    with pytest.raises(RuntimeError, match="timed out"):
        backup.run_backup("pre_migration")
    assert list(directory.iterdir()) == []


def test_rotation_removes_stale_partial_files(env):
    directory, _ = env
    stale = _make_dump(directory, "orbitia_pre_migration_20261008-090000.dump.partial", age_minutes=120)
    fresh = _make_dump(directory, "orbitia_pre_migration_20261008-150000.dump.partial", age_minutes=5)
    backup._rotate(directory, keep_days=7)
    assert not stale.exists()
    assert fresh.exists()
