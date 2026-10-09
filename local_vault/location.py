"""保险库位置、可恢复迁移与跨目录实例保护。"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

from local_vault.core import (Vault, VaultError, VAULT_FORMAT, _atomic_write,
                              _existing_fingerprint, _file_lock, _fingerprint,
                              _parse, _read_raw, _sync_directory, _temp_write)


def installation_directory():
    return (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)


class InstanceLock:
    """每个数据目录只允许一个窗口；迁移时同时保护两个目录。"""
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "application.lock").open("a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise VaultError("此保险库已在另一个窗口打开，或目录不可写。请关闭已有窗口或选择可用位置。") from exc

    def close(self):
        self.file.close()


def _present(path):
    # 只把确实不存在视为空；不可访问不能冒充空库。
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


def _identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def _probe(directory):
    temporary = _temp_write(directory / "vault.lvault", b"")
    temporary.unlink()


class LocationSession:
    def __init__(self, default_directory, legacy_directory=None, config_path=None,
                 recovery_target=None):
        self.default_directory = Path(default_directory).resolve()
        self.legacy_directory = Path(legacy_directory).resolve() if legacy_directory else None
        self.config_path = Path(config_path or
                                (self.legacy_directory or self.default_directory) / "location.json").resolve()
        self.directory = self.default_directory
        self.vault = None
        self.load_error = None
        self.instance = None
        self._stack = ExitStack()
        self._config = None
        try:
            # 同一用户的位置配置只允许一个普通启动会话，防止迁移后另一实例沿用旧路径。
            self._stack.enter_context(_file_lock(self.config_path.with_name("location-session")))
            self._load_config()
            self._recover_pending()
            if self._config is not None:
                self.directory = Path(self._config["directory"])
            elif self.legacy_directory and _present(self.legacy_directory / "vault.lvault"):
                self.directory = self.legacy_directory
            source = self.directory / "vault.lvault"
            exists = _present(source)
            if self._config is not None and self._config["has_vault"] and not exists:
                raise VaultError(f"记住位置中的原保险库无法找到：{source}。请恢复该目录的访问后重试；不会创建空库。")
            if exists:
                self.instance = InstanceLock(self.directory)
                try:
                    self.vault = Vault(source)
                except VaultError as exc:
                    self.vault = Vault.for_restore(source)
                    self.load_error = exc
            elif recovery_target and Path(recovery_target).resolve() != self.directory:
                # 全新使用的默认目录不可写时，允许先选择可用目录。
                self.vault = Vault(source)
            else:
                self.instance = InstanceLock(self.directory)
                _probe(self.directory)
                self.vault = Vault(source)
            target = (Path(recovery_target).resolve() if recovery_target else
                      self.default_directory if self._config is None else self.directory)
            self.vault._location_guard = self.prepare_write
            self.desired_directory = target
            if target != self.directory and self.load_error is None:
                self.change_directory(target, remember=bool(recovery_target))
        except BaseException:
            self.close()
            raise

    def _load_config(self):
        if not _present(self.config_path):
            return
        try:
            raw = self.config_path.read_bytes()
            if len(raw) > 16384:
                raise ValueError()
            value = json.loads(raw)
            if (not isinstance(value, dict) or not isinstance(value.get("directory"), str)
                    or not Path(value["directory"]).is_absolute()
                    or type(value.get("has_vault")) is not bool):
                raise ValueError()
            self._config = value
        except (ValueError, UnicodeError) as exc:
            raise VaultError("保存位置设置无法读取，原数据会保留。请恢复位置设置文件后重试。") from exc

    def _save_config(self, value):
        if value is None:
            self.config_path.unlink(missing_ok=True)
        else:
            raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            _atomic_write(self.config_path, raw, _existing_fingerprint(self.config_path))
            if self.config_path.read_bytes() != raw:
                raise VaultError("保存位置设置写入后验证失败，原库仍保留。")
        self._config = value

    def _recover_pending(self):
        pending = self._config.get("pending") if self._config else None
        if pending is None:
            return
        try:
            source = Path(pending["source"])
            target = Path(self._config["directory"]) / "vault.lvault"
            digest = bytes.fromhex(pending["sha256"])
            if (not source.is_absolute() or source.name != "vault.lvault"
                    or len(digest) != 32 or source.resolve() == target.resolve()):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise VaultError("迁移记录无效；原数据会保留，请恢复位置设置后重试。") from exc
        with ExitStack() as locks:
            for directory in sorted({source.parent, target.parent}, key=str):
                instance = InstanceLock(directory)
                locks.callback(instance.close)
            locks.enter_context(_file_lock(source))
            locks.enter_context(_file_lock(target))
            if _present(source):
                # 删除原库前中断：退回原位置，只清理本次已验证且未被改动的目标。
                if _existing_fingerprint(source) != digest:
                    raise VaultError("未完成迁移的原文件已被修改，两个文件与迁移记录均保留，请检查后重试。")
                if _present(target):
                    if (_identity(target) != pending.get("identity")
                            or _existing_fingerprint(target) != digest):
                        raise VaultError("未完成迁移的目标已被修改，两个文件均保留，请检查后重试。")
                    target.unlink()
                self._save_config(pending.get("previous"))
            else:
                if not _present(target):
                    raise VaultError("未完成迁移的保险库无法访问；不会创建空库。")
                # 初次删除原库后尚未清理记录；目标必须仍是已验证的迁移文件。
                if (_identity(target) != pending.get("identity")
                        or _existing_fingerprint(target) != digest):
                    raise VaultError("未完成迁移的目标已被修改，数据及迁移记录保留，请检查后重试。")
                Vault(target)
                self._save_config(pending.get("final", {"directory": str(target.parent), "has_vault": True}))

    def record_creation(self):
        """先记下新库预期，后续创建失败也不会误开另一个空库。"""
        if self._config is not None and not self._config["has_vault"]:
            self._save_config({"directory": str(self.directory), "has_vault": True})

    def prepare_write(self):
        if self._config is not None and self._config.get("pending"):
            self._save_config(self._config["pending"].get(
                "final", {"directory": str(self.directory), "has_vault": self.vault.is_initialized}))

    def create(self, password):
        previous = self._config
        try:
            self.record_creation()
            self.vault.create(password)
        except (VaultError, OSError):
            if not self.vault.is_initialized:
                self._save_config(previous)
            raise

    def change_directory(self, directory, *, remember=True):
        self.prepare_write()
        target_directory = Path(directory).resolve()
        if target_directory == self.directory:
            return False
        target = target_directory / "vault.lvault"
        source = self.vault.path
        previous = self._config
        final = {"directory": str(target_directory), "has_vault": self.vault.is_initialized} if remember else None
        target_instance = InstanceLock(target_directory)
        temporary = None
        created_identity = None
        raw = None
        committed = False
        try:
            with self.vault._mutex, ExitStack() as locks:
                if self.instance is not None:
                    locks.enter_context(_file_lock(source))
                locks.enter_context(_file_lock(target))
                self.vault._assert_current()
                if _present(target):
                    raise VaultError("目标位置已经存在保险库，不能覆盖或合并。请重新选择其他目录。")
                _probe(target_directory)
                if self.vault.is_initialized:
                    raw = _read_raw(source)
                    _parse(raw, VAULT_FORMAT)
                    temporary = _temp_write(target, raw)
                    # Windows 的 rename 不覆盖已有文件；其他系统用排他硬链接提交。
                    if os.name == "nt":
                        os.rename(temporary, target)
                    else:
                        os.link(temporary, target)
                        temporary.unlink()
                    temporary = None
                    created_identity = _identity(target)
                    _sync_directory(target_directory)
                    if _read_raw(target) != raw:
                        raise VaultError("迁移后的保险库验证失败，原库仍保留。")
                    self.vault._assert_current()
                    self._save_config({"directory": str(target_directory), "has_vault": True,
                                       "pending": {"source": str(source),
                                                   "sha256": _fingerprint(raw).hex(),
                                                   "identity": created_identity, "previous": previous,
                                                   "final": final}})
                    # 设置已持久保存、源与目标再次核对后，才移除原库。
                    self.vault._assert_current()
                    if _read_raw(target) != raw:
                        raise VaultError("目标保险库已被其他程序修改，迁移已取消。")
                    source.unlink()
                    _sync_directory(source.parent)
                else:
                    self._save_config(final)
                committed = True
                self.vault.path = target
                self.vault._plans.clear()
                self.directory = target_directory
                if self.instance is not None:
                    self.instance.close()
                self.instance = target_instance
                target_instance = None
                if raw is not None:
                    # 此时原库已移除。若清理记录失败，重启按记录继续使用目标。
                    try:
                        self._save_config(final)
                    except (VaultError, OSError):
                        pass
                return True
        except (VaultError, OSError) as exc:
            rollback_error = None
            if not committed:
                try:
                    # 即使保存函数在替换配置后抛错，也恢复原配置。
                    self._save_config(previous)
                except (VaultError, OSError) as error:
                    rollback_error = error
                if created_identity is not None:
                    try:
                        if (_present(target) and _identity(target) == created_identity
                                and _read_raw(target) == raw):
                            target.unlink()
                    except (VaultError, OSError) as error:
                        rollback_error = error
            message = f"迁移未完成，原保险库和原位置保留。\n{exc}"
            if rollback_error:
                message += "\n回退清理未完成，请勿手动删除保险库；恢复目录访问后重试。"
            raise VaultError(message) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if target_instance is not None:
                target_instance.close()

    def close(self):
        if self.instance is not None:
            self.instance.close()
            self.instance = None
        self._stack.close()
