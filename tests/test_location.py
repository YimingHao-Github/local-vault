"""只使用隔离目录和虚构正文验证保存位置及可恢复迁移。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from local_vault import location
from local_vault.core import Vault, VaultError, _file_lock
from local_vault.location import InstanceLock, LocationSession


PASSWORD = "保存位置验证专用虚构长口令-84726351"
BODY = "仅供保存位置验证\n  空格保留  \n虚构密钥：FAKE-LOCATION-ONLY\n中文甲乙丙"


def create_fake(path):
    vault = Vault(path)
    vault.create(PASSWORD)
    entry_id = vault.put_entry("虚构位置条目", BODY)
    vault.lock()
    return entry_id


def config_for(root):
    return root / "profile" / "location.json"


@pytest.fixture
def populated_session(tmp_path):
    directory = tmp_path / "installed"
    directory.mkdir()
    entry_id = create_fake(directory / "vault.lvault")
    session = LocationSession(directory, config_path=config_for(tmp_path))
    try:
        yield session, entry_id
    finally:
        session.close()


def assert_original(session, directory, raw):
    assert session.directory == directory
    assert session.vault.path == directory / "vault.lvault"
    assert session.vault.path.read_bytes() == raw
    session.vault.unlock(PASSWORD)
    assert session.vault.get_entry(session.vault.list_titles()[0][0]).body == BODY


def write_pending(config, source, target, previous):
    value = {
        "directory": str(target.parent),
        "has_vault": True,
        "pending": {
            "source": str(source),
            "sha256": location._fingerprint(target.read_bytes()).hex(),
            "identity": location._identity(target),
            "previous": previous,
        },
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value


@contextmanager
def deny_isolated_directory_writes(directory, test_root):
    """只对当前用例的子目录拒绝当前 SID 写入，并始终恢复权限。"""
    if os.name != "nt":
        pytest.skip("真正目录权限拒绝验证仅适用于 Windows")
    icacls = shutil.which("icacls")
    whoami = shutil.which("whoami")
    if not icacls or not whoami:
        pytest.skip("此 Windows 环境缺少 icacls 或 whoami，未执行真实权限验证")
    resolved_root = test_root.resolve(strict=True)
    resolved_directory = directory.resolve(strict=True)
    assert resolved_directory != resolved_root
    assert resolved_root in resolved_directory.parents
    assert resolved_directory.is_dir()
    identity = subprocess.run([whoami, "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=15)
    sid_match = re.search(rb"\bS-\d(?:-\d+)+\b", identity.stdout)
    if identity.returncode != 0 or sid_match is None:
        pytest.skip("无法取得当前进程 SID，未执行真实权限验证")
    sid = sid_match.group().decode("ascii")
    attempted = False
    try:
        attempted = True
        denied = subprocess.run([icacls, str(resolved_directory), "/deny", f"*{sid}:(OI)(CI)(W)"],
                                capture_output=True, timeout=15)
        if denied.returncode != 0:
            pytest.skip("隔离目录不支持设置写入拒绝权限，未执行真实权限验证")
        probe = resolved_directory / "fake-acl-write-probe.txt"
        try:
            with probe.open("xb") as stream:
                stream.write(b"FAKE-ACL-PROBE")
        except PermissionError:
            pass
        else:
            probe.unlink()
            pytest.skip("当前进程未受 icacls 写入拒绝限制，未执行真实权限验证")
        yield
    finally:
        if attempted:
            # 即使设置失败或用例跳过，也只在已核对的隔离子目录移除该 SID 的拒绝项。
            assert resolved_root in resolved_directory.parents
            cleaned = subprocess.run([icacls, str(resolved_directory), "/remove:d", f"*{sid}", "/t", "/c"],
                                     capture_output=True, timeout=15)
            if cleaned.returncode != 0:
                pytest.fail(f"隔离权限恢复失败，必须保留并检查本用例目录：{resolved_directory}")


def test_frozen_default_uses_executable_directory_and_ignores_working_directory(tmp_path, monkeypatch):
    installed = tmp_path / "another-drive" / "installed"
    shortcut_directory = tmp_path / "shortcut-working-directory"
    installed.mkdir(parents=True)
    shortcut_directory.mkdir()
    monkeypatch.setattr(location.sys, "frozen", True, raising=False)
    monkeypatch.setattr(location.sys, "executable", str(installed / "LocalVault.exe"))
    monkeypatch.chdir(shortcut_directory)
    assert location.installation_directory() == installed


def test_source_default_uses_project_directory_and_ignores_python_directory(tmp_path, monkeypatch):
    project = tmp_path / "source"
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.delattr(location.sys, "frozen", raising=False)
    monkeypatch.setattr(location, "__file__", str(project / "local_vault" / "location.py"))
    monkeypatch.setattr(location.sys, "executable", str(tmp_path / "python" / "python.exe"))
    monkeypatch.chdir(working)
    assert location.installation_directory() == project


def test_fresh_default_creates_vault_next_to_installed_program(tmp_path):
    installed = tmp_path / "installed"
    session = LocationSession(installed, config_path=config_for(tmp_path))
    try:
        assert session.directory == installed
        assert session.vault.path == installed / "vault.lvault"
        assert not session.vault.is_initialized
        session.vault.create(PASSWORD)
        session.record_creation()
        assert (installed / "vault.lvault").is_file()
        assert not (tmp_path / "vault.lvault").exists()
    finally:
        session.close()


def test_custom_migration_preserves_exact_bytes_password_body_and_restart(populated_session, tmp_path, monkeypatch):
    session, entry_id = populated_session
    original = session.directory
    raw = session.vault.path.read_bytes()
    session.vault.unlock(PASSWORD)
    target = tmp_path / "chosen" / "中文保存目录"
    assert session.change_directory(target)
    assert session.directory == target
    assert session.vault.path.read_bytes() == raw
    assert not (original / "vault.lvault").exists()
    assert session.vault.get_entry(entry_id).body == BODY
    session.vault.put_entry("迁移后虚构条目", "迁移后继续保存")
    session.close()
    elsewhere = tmp_path / "shortcut"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    reopened = LocationSession(original, config_path=config_for(tmp_path))
    try:
        assert reopened.directory == target
        reopened.vault.unlock(PASSWORD)
        assert reopened.vault.get_entry(entry_id).body == BODY
        assert [title for _, title in reopened.vault.list_titles()] == ["虚构位置条目", "迁移后虚构条目"]
    finally:
        reopened.close()


def test_automatic_legacy_migration_moves_only_vault_and_preserves_bytes(tmp_path):
    installed = tmp_path / "installed"
    legacy = tmp_path / "legacy-profile" / "LocalVault"
    legacy.mkdir(parents=True)
    entry_id = create_fake(legacy / "vault.lvault")
    raw = (legacy / "vault.lvault").read_bytes()
    unrelated = {"vault.lvault.bak": b"FAKE-OLD-HISTORY", "legacy.lvexport": b"FAKE-EXPORT",
                 "legacy.lvbackup": b"FAKE-BACKUP", "unrelated.txt": b"FAKE-UNRELATED"}
    for name, content in unrelated.items():
        (legacy / name).write_bytes(content)
    session = LocationSession(installed, legacy, config_for(tmp_path))
    try:
        assert session.directory == installed
        assert (installed / "vault.lvault").read_bytes() == raw
        assert not (legacy / "vault.lvault").exists()
        for name, content in unrelated.items():
            assert (legacy / name).read_bytes() == content
            assert not (installed / name).exists()
        session.vault.unlock(PASSWORD)
        assert session.vault.get_entry(entry_id).body == BODY
    finally:
        session.close()


def test_automatic_legacy_migration_keeps_default_relative_to_current_installation(tmp_path):
    first_installation, second_installation = tmp_path / "first-installed", tmp_path / "second-installed"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    create_fake(legacy / "vault.lvault")
    original = (legacy / "vault.lvault").read_bytes()
    config = config_for(tmp_path)
    first = LocationSession(first_installation, legacy, config)
    try:
        assert first.directory == first_installation
        assert first.vault.path.read_bytes() == original
        assert not config.exists()
    finally:
        first.close()
    # 程序的实际安装目录改变时，未选自定义位置的默认值仍随本次程序确定。
    second = LocationSession(second_installation, legacy, config)
    try:
        assert second.directory == second_installation
        assert second.vault.path == second_installation / "vault.lvault"
        assert not second.vault.is_initialized
        assert not config.exists()
        assert (first_installation / "vault.lvault").read_bytes() == original
        assert not (legacy / "vault.lvault").exists()
    finally:
        second.close()


def test_custom_location_stays_remembered_when_installation_directory_changes(populated_session, tmp_path):
    session, entry_id = populated_session
    custom = tmp_path / "explicit-custom"
    session.change_directory(custom)
    raw = session.vault.path.read_bytes()
    session.close()
    reopened = LocationSession(tmp_path / "another-installed", config_path=config_for(tmp_path))
    try:
        assert reopened.directory == custom
        assert reopened.vault.path.read_bytes() == raw
        reopened.vault.unlock(PASSWORD)
        assert reopened.vault.get_entry(entry_id).body == BODY
        assert not (tmp_path / "another-installed" / "vault.lvault").exists()
    finally:
        reopened.close()


def test_remembered_custom_location_prevents_legacy_discovery(populated_session, tmp_path):
    session, _ = populated_session
    installed = session.directory
    custom = tmp_path / "custom"
    session.change_directory(custom)
    session.close()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "vault.lvault").write_bytes(b"FAKE-UNRELATED-LEGACY-FILE")
    reopened = LocationSession(installed, legacy, config_for(tmp_path))
    try:
        assert reopened.directory == custom
        assert (legacy / "vault.lvault").read_bytes() == b"FAKE-UNRELATED-LEGACY-FILE"
    finally:
        reopened.close()


def test_same_directory_and_relative_alias_do_not_change_vault_or_config(populated_session):
    session, _ = populated_session
    directory = session.directory
    raw = session.vault.path.read_bytes()
    assert not session.change_directory(directory)
    assert not session.change_directory(directory / ".." / directory.name)
    assert_original(session, directory, raw)
    assert not session.config_path.exists()


@pytest.mark.parametrize("identical", [False, True])
def test_target_existing_vault_is_never_overwritten_even_if_bytes_identical(populated_session, tmp_path, identical):
    session, _ = populated_session
    directory = session.directory
    raw = session.vault.path.read_bytes()
    target = tmp_path / "conflicting"
    target.mkdir()
    foreign = raw if identical else b"FAKE-OTHER-VAULT-BYTES"
    (target / "vault.lvault").write_bytes(foreign)
    with pytest.raises(VaultError, match="已经存在保险库"):
        session.change_directory(target)
    assert_original(session, directory, raw)
    assert (target / "vault.lvault").read_bytes() == foreign
    assert not session.config_path.exists()


def test_hard_link_target_conflict_cannot_delete_original(populated_session, tmp_path):
    session, _ = populated_session
    directory = session.directory
    raw = session.vault.path.read_bytes()
    target = tmp_path / "hard-link-target"
    target.mkdir()
    os.link(session.vault.path, target / "vault.lvault")
    with pytest.raises(VaultError, match="已经存在保险库"):
        session.change_directory(target)
    assert_original(session, directory, raw)
    assert (target / "vault.lvault").read_bytes() == raw


def test_automatic_legacy_conflict_preserves_both_files(tmp_path):
    installed, legacy = tmp_path / "installed", tmp_path / "legacy"
    installed.mkdir()
    legacy.mkdir()
    create_fake(legacy / "vault.lvault")
    raw = (legacy / "vault.lvault").read_bytes()
    (installed / "vault.lvault").write_bytes(b"FAKE-OTHER-INSTALLED-VAULT")
    with pytest.raises(VaultError, match="已经存在保险库"):
        LocationSession(installed, legacy, config_for(tmp_path))
    assert (legacy / "vault.lvault").read_bytes() == raw
    assert (installed / "vault.lvault").read_bytes() == b"FAKE-OTHER-INSTALLED-VAULT"


def test_target_created_during_commit_is_never_overwritten(populated_session, tmp_path, monkeypatch):
    session, _ = populated_session
    directory, raw = session.directory, session.vault.path.read_bytes()
    target_directory = tmp_path / "racing-target"
    target = target_directory / "vault.lvault"
    operation = "rename" if os.name == "nt" else "link"
    saved_commit = getattr(location.os, operation)
    foreign = b"FAKE-CONCURRENT-TARGET-VAULT"

    def racing_commit(temporary, destination):
        if Path(destination) == target:
            target.write_bytes(foreign)
        return saved_commit(temporary, destination)

    monkeypatch.setattr(location.os, operation, racing_commit)
    with pytest.raises(VaultError, match="原保险库和原位置保留"):
        session.change_directory(target_directory)
    assert_original(session, directory, raw)
    assert target.read_bytes() == foreign
    assert not session.config_path.exists()
    assert not list(target_directory.glob(".vault.lvault.*.tmp"))


@pytest.mark.parametrize("changed_file", ["source", "target"])
def test_external_change_after_copy_cannot_remove_source_or_changed_target(populated_session, tmp_path, monkeypatch, changed_file):
    session, _ = populated_session
    directory, raw = session.directory, session.vault.path.read_bytes()
    source = session.vault.path
    target_directory = tmp_path / "changed-target"
    target = target_directory / "vault.lvault"
    saved_save = session._save_config
    changed = raw + b"\n"

    def change_after_copy(value):
        saved_save(value)
        if value is not None and "pending" in value:
            (source if changed_file == "source" else target).write_bytes(changed)

    monkeypatch.setattr(session, "_save_config", change_after_copy)
    with pytest.raises(VaultError, match="修改"):
        session.change_directory(target_directory)
    assert session.directory == directory
    assert source.read_bytes() == (changed if changed_file == "source" else raw)
    assert not session.config_path.exists()
    if changed_file == "source":
        assert not target.exists()
    else:
        assert target.read_bytes() == changed


def test_directory_file_is_rejected_without_losing_source(populated_session, tmp_path):
    session, _ = populated_session
    raw, directory = session.vault.path.read_bytes(), session.directory
    target = tmp_path / "not-a-directory"
    target.write_bytes(b"FAKE-DIRECTORY-CONFLICT")
    with pytest.raises((VaultError, OSError)):
        session.change_directory(target)
    assert_original(session, directory, raw)
    assert target.read_bytes() == b"FAKE-DIRECTORY-CONFLICT"


@pytest.mark.parametrize("stage", ["probe", "temporary", "rename", "verify", "config", "config_after_commit", "unlink"])
def test_migration_failure_keeps_original_data_and_session(populated_session, tmp_path, monkeypatch, stage):
    session, _ = populated_session
    directory, raw = session.directory, session.vault.path.read_bytes()
    source = session.vault.path
    target_directory = tmp_path / "failed-target"
    target = target_directory / "vault.lvault"
    saved_temp_write = location._temp_write
    saved_read = location._read_raw
    saved_atomic_write = location._atomic_write
    saved_unlink = Path.unlink

    def failed_temp_write(path, data):
        if path == target and ((stage == "probe" and data == b"") or (stage == "temporary" and data != b"")):
            raise VaultError("虚构磁盘无权限或空间不足")
        return saved_temp_write(path, data)

    def failed_read(path):
        if stage == "verify" and path == target:
            return b"FAKE-VERIFICATION-MISMATCH"
        return saved_read(path)

    def failed_atomic_write(path, data, expected):
        if path == session.config_path and b'"pending"' in data:
            if stage == "config_after_commit":
                saved_atomic_write(path, data, expected)
                raise OSError("虚构配置提交后异常")
            if stage == "config":
                raise OSError("虚构配置保存失败")
        return saved_atomic_write(path, data, expected)

    def failed_unlink(path, *args, **kwargs):
        if stage == "unlink" and path == source:
            raise PermissionError("虚构原库删除无权限")
        return saved_unlink(path, *args, **kwargs)

    monkeypatch.setattr(location, "_temp_write", failed_temp_write)
    monkeypatch.setattr(location, "_read_raw", failed_read)
    monkeypatch.setattr(location, "_atomic_write", failed_atomic_write)
    monkeypatch.setattr(Path, "unlink", failed_unlink)
    if stage == "rename":
        operation = "rename" if os.name == "nt" else "link"
        monkeypatch.setattr(location.os, operation, lambda *args, **kwargs: (_ for _ in ()).throw(OSError("虚构迁移提交失败")))
    with pytest.raises(VaultError, match="原保险库和原位置保留"):
        session.change_directory(target_directory)
    assert_original(session, directory, raw)
    assert not session.config_path.exists()
    if stage != "verify":
        assert not target.exists()
    assert not list(target_directory.glob(".vault.lvault.*.tmp"))
    assert not session.change_directory(directory)
    # 失败后目标的实例保护也应释放。
    other = InstanceLock(target_directory)
    other.close()


def test_external_source_change_is_detected_before_migration(populated_session, tmp_path):
    session, _ = populated_session
    directory = session.directory
    external = Vault(session.vault.path)
    external.unlock(PASSWORD)
    external.put_entry("外部虚构条目", "外部程序已保存的虚构内容")
    raw = session.vault.path.read_bytes()
    with pytest.raises(VaultError, match="其他程序修改"):
        session.change_directory(tmp_path / "target")
    assert session.directory == directory
    assert session.vault.path.read_bytes() == raw
    assert not (tmp_path / "target" / "vault.lvault").exists()


def test_source_instance_lock_blocks_automatic_legacy_migration(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    create_fake(legacy / "vault.lvault")
    raw = (legacy / "vault.lvault").read_bytes()
    lock = InstanceLock(legacy)
    try:
        with pytest.raises(VaultError, match="另一个窗口"):
            LocationSession(tmp_path / "installed", legacy, config_for(tmp_path))
        assert (legacy / "vault.lvault").read_bytes() == raw
        assert not (tmp_path / "installed" / "vault.lvault").exists()
    finally:
        lock.close()


@pytest.mark.parametrize("kind", ["instance", "source_write", "target_write"])
def test_migration_respects_source_and_target_locks(populated_session, tmp_path, kind):
    session, _ = populated_session
    directory, raw = session.directory, session.vault.path.read_bytes()
    target = tmp_path / "locked-target"
    target.mkdir()
    if kind == "instance":
        lock = InstanceLock(target)
        try:
            with pytest.raises(VaultError, match="另一个窗口"):
                session.change_directory(target)
        finally:
            lock.close()
    else:
        path = session.vault.path if kind == "source_write" else target / "vault.lvault"
        with _file_lock(path):
            with pytest.raises(VaultError, match="其他程序使用"):
                session.change_directory(target)
    assert_original(session, directory, raw)
    assert not (target / "vault.lvault").exists()


def test_configuration_session_lock_blocks_other_default_directories(populated_session, tmp_path):
    session, _ = populated_session
    second_directory = tmp_path / "second-installed"
    with pytest.raises(VaultError, match="其他程序使用"):
        LocationSession(second_directory, config_path=session.config_path)
    assert not (second_directory / "vault.lvault").exists()
    session.close()
    reopened = LocationSession(second_directory, config_path=session.config_path)
    reopened.close()


def test_successful_migration_holds_target_instance_lock_and_releases_source(populated_session, tmp_path):
    session, _ = populated_session
    source = session.directory
    target = tmp_path / "target"
    session.change_directory(target)
    old_lock = InstanceLock(source)
    old_lock.close()
    with pytest.raises(VaultError, match="另一个窗口"):
        InstanceLock(target)
    session.close()
    new_lock = InstanceLock(target)
    new_lock.close()


def test_source_and_target_instance_locks_remain_held_before_source_deletion(populated_session, tmp_path, monkeypatch):
    session, _ = populated_session
    source = session.directory
    target = tmp_path / "target"
    saved_save_config = session._save_config
    observed = []

    def inspect_locks(value):
        if value is not None and "pending" in value:
            for directory in (source, target):
                with pytest.raises(VaultError, match="另一个窗口"):
                    InstanceLock(directory)
            assert (source / "vault.lvault").is_file()
            assert (target / "vault.lvault").is_file()
            observed.append(True)
        saved_save_config(value)

    monkeypatch.setattr(session, "_save_config", inspect_locks)
    session.change_directory(target)
    assert observed == [True]


def test_remembered_existing_vault_missing_is_not_replaced_with_empty_vault(tmp_path):
    custom = tmp_path / "missing-custom"
    config = config_for(tmp_path)
    config.parent.mkdir()
    value = {"directory": str(custom), "has_vault": True}
    config.write_text(json.dumps(value), encoding="utf-8")
    before = config.read_bytes()
    with pytest.raises(VaultError, match="不会创建空库"):
        LocationSession(tmp_path / "installed", config_path=config)
    assert not (custom / "vault.lvault").exists()
    assert not (tmp_path / "installed" / "vault.lvault").exists()
    assert config.read_bytes() == before


def test_unreadable_saved_vault_does_not_fall_back_or_overwrite(tmp_path, monkeypatch):
    custom = tmp_path / "custom"
    custom.mkdir()
    create_fake(custom / "vault.lvault")
    config = config_for(tmp_path)
    config.parent.mkdir()
    config.write_text(json.dumps({"directory": str(custom), "has_vault": True}), encoding="utf-8")
    raw = (custom / "vault.lvault").read_bytes()
    original_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == custom / "vault.lvault":
            raise PermissionError("虚构保险库访问无权限")
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", denied_stat)
        with pytest.raises((VaultError, OSError)):
            LocationSession(tmp_path / "installed", config_path=config)
    assert (custom / "vault.lvault").read_bytes() == raw
    assert not (tmp_path / "installed" / "vault.lvault").exists()


def test_fresh_unwritable_default_can_select_isolated_recovery_directory(tmp_path, monkeypatch):
    installed, selected = tmp_path / "installed", tmp_path / "selected"
    saved_instance_lock = location.InstanceLock
    saved_file_lock = location._file_lock

    def denied_default(directory):
        if Path(directory) == installed:
            raise PermissionError("虚构安装目录不可写")
        return saved_instance_lock(directory)

    @contextmanager
    def denied_default_file_lock(path):
        if Path(path).parent == installed:
            raise PermissionError("虚构安装目录连空锁文件也不能写入")
        with saved_file_lock(path):
            yield

    monkeypatch.setattr(location, "InstanceLock", denied_default)
    monkeypatch.setattr(location, "_file_lock", denied_default_file_lock)
    session = LocationSession(installed, config_path=config_for(tmp_path), recovery_target=selected)
    try:
        assert session.directory == selected
        assert not session.vault.is_initialized
        session.record_creation()
        session.vault.create(PASSWORD)
        assert (selected / "vault.lvault").is_file()
        assert not (installed / "vault.lvault").exists()
    finally:
        session.close()


def test_fresh_unwritable_default_reports_failure_without_creating_vault(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    saved_instance_lock = location.InstanceLock

    def denied_default(directory):
        if Path(directory) == installed:
            raise PermissionError("虚构安装目录不可写")
        return saved_instance_lock(directory)

    monkeypatch.setattr(location, "InstanceLock", denied_default)
    with pytest.raises((VaultError, OSError), match="不可写"):
        LocationSession(installed, config_path=config_for(tmp_path))
    assert not (installed / "vault.lvault").exists()
    assert not config_for(tmp_path).exists()


def test_windows_actual_unwritable_default_reports_and_uses_selected_directory_after_restart(tmp_path):
    installed, selected = tmp_path / "denied-installed", tmp_path / "selected"
    installed.mkdir()
    config = config_for(tmp_path)
    with deny_isolated_directory_writes(installed, tmp_path):
        with pytest.raises((VaultError, OSError)):
            LocationSession(installed, config_path=config)
        with pytest.raises((VaultError, OSError)):
            LocationSession(installed, config_path=config, recovery_target=installed)
        assert not (installed / "vault.lvault").exists()
        assert not config.exists()
        session = LocationSession(installed, config_path=config, recovery_target=selected)
        try:
            assert session.directory == selected
            session.create(PASSWORD)
            entry_id = session.vault.put_entry("真正目录权限虚构条目", BODY)
            raw = session.vault.path.read_bytes()
        finally:
            session.close()
        reopened = LocationSession(installed, config_path=config)
        try:
            assert reopened.directory == selected
            assert reopened.vault.path.read_bytes() == raw
            reopened.vault.unlock(PASSWORD)
            assert reopened.vault.get_entry(entry_id).body == BODY
            assert not (installed / "vault.lvault").exists()
        finally:
            reopened.close()
    # 真正写入也证明 finally 已恢复本用例目录的权限。
    (installed / "fake-acl-cleanup-proof.txt").write_bytes(b"FAKE-ACL-CLEANUP")


def test_same_directory_recovery_selection_still_holds_instance_lock(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    session = LocationSession(installed, config_path=config_for(tmp_path), recovery_target=installed)
    try:
        assert session.directory == installed
        assert session.instance is not None
        assert not session.vault.is_initialized
        with pytest.raises(VaultError, match="另一个窗口"):
            InstanceLock(installed)
    finally:
        session.close()
    lock = InstanceLock(installed)
    lock.close()


def test_windows_actual_denied_migration_target_preserves_source_and_setting(populated_session, tmp_path):
    session, _ = populated_session
    source_directory = tmp_path / "remembered-source"
    session.change_directory(source_directory)
    raw = session.vault.path.read_bytes()
    config_before = session.config_path.read_bytes()
    target = tmp_path / "denied-target"
    target.mkdir()
    with deny_isolated_directory_writes(target, tmp_path):
        with pytest.raises((VaultError, OSError)):
            session.change_directory(target)
        assert_original(session, source_directory, raw)
        assert session.config_path.read_bytes() == config_before
        assert not (target / "vault.lvault").exists()
    (target / "fake-acl-cleanup-proof.txt").write_bytes(b"FAKE-ACL-CLEANUP")


def test_custom_initial_creation_failure_can_retry_and_restart(tmp_path, monkeypatch):
    installed, custom = tmp_path / "installed", tmp_path / "custom"
    session = LocationSession(installed, config_path=config_for(tmp_path))
    try:
        session.change_directory(custom)
        previous = json.loads(session.config_path.read_text(encoding="utf-8"))
        with monkeypatch.context() as patch:
            patch.setattr(session.vault, "create", lambda password: (_ for _ in ()).throw(VaultError("虚构初次保存失败")))
            with pytest.raises(VaultError, match="初次保存失败"):
                session.create(PASSWORD)
        assert json.loads(session.config_path.read_text(encoding="utf-8")) == previous
        assert not (custom / "vault.lvault").exists()
        session.create(PASSWORD)
        session.vault.put_entry("虚构创建重试", BODY)
    finally:
        session.close()
    reopened = LocationSession(installed, config_path=config_for(tmp_path))
    try:
        assert reopened.directory == custom
        reopened.vault.unlock(PASSWORD)
        assert reopened.vault.get_entry(reopened.vault.list_titles()[0][0]).body == BODY
    finally:
        reopened.close()


def test_creation_expectation_config_error_after_commit_restores_uninitialized_state(tmp_path, monkeypatch):
    installed, custom = tmp_path / "installed", tmp_path / "custom"
    config = config_for(tmp_path)
    session = LocationSession(installed, config_path=config)
    try:
        session.change_directory(custom)
        previous = json.loads(config.read_text(encoding="utf-8"))
        saved_atomic_write = location._atomic_write

        def fail_record_after_commit(path, raw, expected):
            saved_atomic_write(path, raw, expected)
            if path == config and json.loads(raw).get("has_vault") is True:
                raise OSError("虚构创建预期配置提交后验证失败")

        with monkeypatch.context() as patch:
            patch.setattr(location, "_atomic_write", fail_record_after_commit)
            with pytest.raises((VaultError, OSError), match="验证失败"):
                session.create(PASSWORD)
        assert json.loads(config.read_text(encoding="utf-8")) == previous
        assert not session.vault.is_initialized
        assert not (custom / "vault.lvault").exists()
    finally:
        session.close()
    reopened = LocationSession(installed, config_path=config)
    try:
        assert reopened.directory == custom
        assert not reopened.vault.is_initialized
        reopened.create(PASSWORD)
        assert (custom / "vault.lvault").is_file()
    finally:
        reopened.close()


def test_migration_failure_from_custom_location_restores_previous_setting(populated_session, tmp_path, monkeypatch):
    session, _ = populated_session
    first_custom = tmp_path / "first-custom"
    session.change_directory(first_custom)
    raw = session.vault.path.read_bytes()
    before = json.loads(session.config_path.read_text(encoding="utf-8"))
    saved_atomic_write = location._atomic_write

    def fail_next_config(path, data, expected):
        if path == session.config_path and b'"pending"' in data:
            raise OSError("虚构第二次位置配置保存失败")
        return saved_atomic_write(path, data, expected)

    monkeypatch.setattr(location, "_atomic_write", fail_next_config)
    with pytest.raises(VaultError, match="原位置保留"):
        session.change_directory(tmp_path / "second-custom")
    assert_original(session, first_custom, raw)
    assert json.loads(session.config_path.read_text(encoding="utf-8")) == before
    session.close()
    reopened = LocationSession(tmp_path / "installed", config_path=config_for(tmp_path))
    try:
        assert reopened.directory == first_custom
    finally:
        reopened.close()


def test_custom_uninitialized_location_records_creation_expectation(tmp_path):
    installed, custom = tmp_path / "installed", tmp_path / "custom"
    session = LocationSession(installed, config_path=config_for(tmp_path))
    try:
        session.change_directory(custom)
        assert json.loads(session.config_path.read_text(encoding="utf-8"))["has_vault"] is False
        session.record_creation()
        assert json.loads(session.config_path.read_text(encoding="utf-8"))["has_vault"] is True
    finally:
        session.close()
    with pytest.raises(VaultError, match="不会创建空库"):
        LocationSession(installed, config_path=config_for(tmp_path))


@pytest.mark.parametrize("has_previous", [False, True])
def test_pending_migration_with_source_present_rolls_back_safely(tmp_path, has_previous):
    source_directory, target_directory = tmp_path / "source", tmp_path / "target"
    source_directory.mkdir()
    target_directory.mkdir()
    source, target = source_directory / "vault.lvault", target_directory / "vault.lvault"
    create_fake(source)
    raw = source.read_bytes()
    target.write_bytes(raw)
    previous = {"directory": str(source_directory), "has_vault": True} if has_previous else None
    config = config_for(tmp_path)
    write_pending(config, source, target, previous)
    session = LocationSession(source_directory, config_path=config)
    try:
        assert session.directory == source_directory
        assert source.read_bytes() == raw
        assert not target.exists()
        if has_previous:
            assert json.loads(config.read_text(encoding="utf-8")) == previous
        else:
            assert not config.exists()
        session.vault.unlock(PASSWORD)
        assert session.vault.get_entry(session.vault.list_titles()[0][0]).body == BODY
    finally:
        session.close()


def test_pending_migration_with_source_removed_finishes_at_verified_target(tmp_path):
    source_directory, target_directory = tmp_path / "source", tmp_path / "target"
    source_directory.mkdir()
    target_directory.mkdir()
    source, target = source_directory / "vault.lvault", target_directory / "vault.lvault"
    create_fake(target)
    raw = target.read_bytes()
    config = config_for(tmp_path)
    write_pending(config, source, target, None)
    session = LocationSession(source_directory, config_path=config)
    try:
        assert session.directory == target_directory
        assert target.read_bytes() == raw
        assert not source.exists()
        assert json.loads(config.read_text(encoding="utf-8")) == {"directory": str(target_directory), "has_vault": True}
        session.vault.unlock(PASSWORD)
        assert session.vault.get_entry(session.vault.list_titles()[0][0]).body == BODY
    finally:
        session.close()


def test_pending_automatic_completion_does_not_remember_previous_installation(tmp_path):
    installation, legacy = tmp_path / "installed", tmp_path / "legacy"
    installation.mkdir()
    legacy.mkdir()
    source, target = legacy / "vault.lvault", installation / "vault.lvault"
    create_fake(target)
    raw = target.read_bytes()
    config = config_for(tmp_path)
    value = write_pending(config, source, target, None)
    value["pending"]["final"] = None
    config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    session = LocationSession(installation, legacy, config)
    try:
        assert session.directory == installation
        assert session.vault.path.read_bytes() == raw
        assert not config.exists()
    finally:
        session.close()
    new_installation = tmp_path / "new-installed"
    reopened = LocationSession(new_installation, legacy, config)
    try:
        assert reopened.directory == new_installation
        assert not reopened.vault.is_initialized
        assert target.read_bytes() == raw
    finally:
        reopened.close()


@pytest.mark.parametrize("source_exists", [False, True])
def test_pending_changed_target_preserves_files_and_rejects_recovery(tmp_path, source_exists):
    source_directory, target_directory = tmp_path / "source", tmp_path / "target"
    source_directory.mkdir()
    target_directory.mkdir()
    source, target = source_directory / "vault.lvault", target_directory / "vault.lvault"
    create_fake(target)
    original = target.read_bytes()
    if source_exists:
        source.write_bytes(original)
    config = config_for(tmp_path)
    write_pending(config, source, target, None)
    # 仍为结构完整且可解锁的库，但内容已不同于迁移时确认的摘要。
    foreign = Vault(target)
    foreign.unlock(PASSWORD)
    foreign.put_entry("断电后外部虚构条目", "迁移记录不能擅自接受此修改")
    changed = target.read_bytes()
    config_before = config.read_bytes()
    with pytest.raises(VaultError, match="修改|验证|不一致"):
        LocationSession(source_directory, config_path=config)
    assert target.read_bytes() == changed
    assert config.read_bytes() == config_before
    assert source.exists() is source_exists
    if source_exists:
        assert source.read_bytes() == original


def test_pending_changed_source_cannot_remove_verified_original_target(tmp_path):
    source_directory, target_directory = tmp_path / "source", tmp_path / "target"
    source_directory.mkdir()
    target_directory.mkdir()
    source, target = source_directory / "vault.lvault", target_directory / "vault.lvault"
    create_fake(source)
    original = source.read_bytes()
    target.write_bytes(original)
    config = config_for(tmp_path)
    write_pending(config, source, target, None)
    changed = b"FAKE-SOURCE-CORRUPTED-AFTER-INTERRUPTED-MIGRATION"
    source.write_bytes(changed)
    config_before = config.read_bytes()
    with pytest.raises(VaultError, match="修改|验证|不一致"):
        session = LocationSession(source_directory, config_path=config)
        session.close()
    assert source.read_bytes() == changed
    assert target.read_bytes() == original
    assert config.read_bytes() == config_before


def test_pending_both_vaults_missing_does_not_create_empty_vault(tmp_path):
    source_directory, target_directory = tmp_path / "source", tmp_path / "target"
    source_directory.mkdir()
    target_directory.mkdir()
    source, target = source_directory / "vault.lvault", target_directory / "vault.lvault"
    create_fake(target)
    config = config_for(tmp_path)
    write_pending(config, source, target, None)
    target.unlink()
    before = config.read_bytes()
    with pytest.raises(VaultError, match="不会创建空库"):
        LocationSession(source_directory, config_path=config)
    assert not source.exists() and not target.exists()
    assert config.read_bytes() == before


def test_final_config_cleanup_failure_recovers_successful_migration_on_restart(populated_session, tmp_path, monkeypatch):
    session, entry_id = populated_session
    original, raw = session.directory, session.vault.path.read_bytes()
    target = tmp_path / "target"
    saved_save = session._save_config

    def fail_final_cleanup(value):
        if value is not None and value.get("directory") == str(target) and "pending" not in value:
            raise OSError("虚构迁移完成记录清理失败")
        saved_save(value)

    monkeypatch.setattr(session, "_save_config", fail_final_cleanup)
    assert session.change_directory(target)
    assert not (original / "vault.lvault").exists()
    assert (target / "vault.lvault").read_bytes() == raw
    assert "pending" in json.loads(session.config_path.read_text(encoding="utf-8"))
    session.close()
    reopened = LocationSession(original, config_path=config_for(tmp_path))
    try:
        assert reopened.directory == target
        reopened.vault.unlock(PASSWORD)
        assert reopened.vault.get_entry(entry_id).body == BODY
        assert "pending" not in json.loads(reopened.config_path.read_text(encoding="utf-8"))
    finally:
        reopened.close()


def test_pending_cleanup_failure_blocks_edits_then_allows_retry_and_restart(populated_session, tmp_path, monkeypatch):
    session, _ = populated_session
    original, raw = session.directory, session.vault.path.read_bytes()
    target = tmp_path / "target"
    saved_save = session._save_config

    def fail_final_cleanup(value):
        if value is not None and value.get("directory") == str(target) and "pending" not in value:
            raise OSError("虚构迁移记录清理失败")
        saved_save(value)

    with monkeypatch.context() as patch:
        patch.setattr(session, "_save_config", fail_final_cleanup)
        assert session.change_directory(target)
        session.vault.unlock(PASSWORD)
        with pytest.raises((VaultError, OSError), match="清理失败"):
            session.vault.put_entry("不能误提交的虚构编辑", "虚构失败正文")
        assert session.vault.path.read_bytes() == raw
        assert [title for _, title in session.vault.list_titles()] == ["虚构位置条目"]
        assert "pending" in json.loads(session.config_path.read_text(encoding="utf-8"))
    session.vault.put_entry("成功重试的虚构编辑", "虚构重试正文")
    assert "pending" not in json.loads(session.config_path.read_text(encoding="utf-8"))
    session.close()
    reopened = LocationSession(original, config_path=config_for(tmp_path))
    try:
        reopened.vault.unlock(PASSWORD)
        assert [title for _, title in reopened.vault.list_titles()] == ["虚构位置条目", "成功重试的虚构编辑"]
    finally:
        reopened.close()


def test_corrupt_legacy_vault_is_preserved_for_existing_restore_flow(tmp_path):
    installed, legacy = tmp_path / "installed", tmp_path / "legacy"
    legacy.mkdir()
    source = legacy / "vault.lvault"
    raw = b"FAKE-CORRUPTED-VAULT-FOR-RECOVERY"
    source.write_bytes(raw)
    session = LocationSession(installed, legacy, config_for(tmp_path))
    try:
        assert isinstance(session.load_error, VaultError)
        assert session.directory == legacy
        assert session.desired_directory == installed
        assert session.vault.path == source
        assert source.read_bytes() == raw
        assert not (installed / "vault.lvault").exists()
        with pytest.raises(VaultError, match="不能创建空保险库"):
            session.create(PASSWORD)
        assert source.read_bytes() == raw
    finally:
        session.close()


def test_recovered_legacy_vault_can_migrate_without_changing_backup_or_password(tmp_path):
    installed, legacy = tmp_path / "installed", tmp_path / "legacy"
    legacy.mkdir()
    source = legacy / "vault.lvault"
    backup = tmp_path / "fake.lvbackup"
    entry_id = create_fake(source)
    original = source.read_bytes()
    valid = Vault(source)
    valid.backup_file(backup)
    source.write_bytes(b"FAKE-CORRUPTED-VAULT-FOR-RECOVERY")
    session = LocationSession(installed, legacy, config_for(tmp_path))
    try:
        session.vault.restore_backup(backup, PASSWORD)
        assert session.change_directory(session.desired_directory)
        assert session.directory == installed
        assert session.vault.get_entry(entry_id).body == BODY
        assert session.vault.path.read_bytes() == original
        assert backup.read_bytes() == original
        assert not source.exists()
    finally:
        session.close()


@pytest.mark.parametrize("content", [b"{", b'{}', b'{"directory":"relative","has_vault":true}',
                                     b'{"directory":"D:/FAKE","has_vault":"yes"}', b"x" * 16385])
def test_invalid_location_config_is_preserved_without_empty_vault(tmp_path, content):
    config = config_for(tmp_path)
    config.parent.mkdir()
    config.write_bytes(content)
    with pytest.raises(VaultError, match="设置无法读取"):
        LocationSession(tmp_path / "installed", config_path=config)
    assert config.read_bytes() == content
    assert not (tmp_path / "installed" / "vault.lvault").exists()
