"""仅在隔离目录中验证保存位置界面，所有保险库及口令均为虚构。"""
import os
import time
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

import pytest

import local_vault.app as app_module
import local_vault.location as location_module
from local_vault.app import MergeDialog, VaultApp, VaultDirectoryDialog
from local_vault.core import Vault
from local_vault.location import LocationSession


PASSWORD = "虚构位置测试主密码-123-完整长口令"
EXPORT_PASSWORD = "虚构位置测试导出密码-123-完整长口令"


@pytest.fixture(scope="module")
def tk_host():
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        pytest.skip("此测试需要桌面显示环境")
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tmp_path, tk_host):
    root = tk.Toplevel(tk_host)
    root.withdraw()
    previous_callbacks = set(root.tk.call("after", "info"))
    session = LocationSession(tmp_path / "install", tmp_path / "profile")
    session.vault.create(PASSWORD)
    application = VaultApp(root, session.vault, session)
    yield application
    for callback in set(root.tk.call("after", "info")) - previous_callbacks:
        root.after_cancel(callback)
    for event in ("<KeyPress>", "<ButtonPress>", "<MouseWheel>"):
        root.unbind_all(event)
    for window in tuple(application.dialogs):
        if window.winfo_exists():
            window.destroy()
    root.destroy()
    session.close()


def widgets(parent):
    """读取实际组件，包含设置窗口内的嵌套容器。"""
    for child in parent.winfo_children():
        yield child
        yield from widgets(child)


def stored_config(app):
    path = app.location.config_path
    return path.read_bytes() if path.exists() else None


def save_fake_entry(app):
    entry_id = app.vault.put_entry("虚构位置条目", "原正文\r\n  AbC 与空格  \n")
    app.set_editor(app.vault.get_entry(entry_id))
    return entry_id


def test_actual_settings_shows_location_and_updates_after_move(app, tmp_path):
    entry_id = save_fake_entry(app)
    source = app.vault.path
    original = source.read_bytes()
    target = tmp_path / "custom"
    target.mkdir()
    observed = []
    errors = []

    def inspect_and_move():
        window = next(iter(app.dialogs))
        try:
            children = list(widgets(window))
            entries = [widget for widget in children if isinstance(widget, ttk.Entry)
                       and str(widget.cget("state")) == "readonly"]
            assert len(entries) == 1
            assert entries[0].get() == str(source)
            buttons = {widget.cget("text"): widget for widget in children
                       if isinstance(widget, ttk.Button)}
            assert "更改保存位置…" in buttons and "保存设置" in buttons
            buttons["更改保存位置…"].invoke()
            assert entries[0].get() == str(target / "vault.lvault")
            assert window.grab_current() == window
            observed.append(entries[0].get())
        except BaseException as exc:
            errors.append(exc)
        finally:
            if window.winfo_exists():
                window.destroy()

    app.root.after(30, inspect_and_move)
    with patch("local_vault.app.filedialog.askdirectory", return_value=str(target)):
        app.settings()
    if errors:
        raise errors[0]
    assert observed == [str(target / "vault.lvault")]
    assert not source.exists()
    assert app.vault.path.read_bytes() == original
    assert app.vault.get_entry(entry_id).body == "原正文\r\n  AbC 与空格  \n"
    assert not app.dialogs and app.modal_depth == 0


@pytest.mark.parametrize("choice", [True, False, None], ids=["save", "discard", "cancel"])
def test_change_location_preserves_existing_unsaved_choices(app, tmp_path, choice):
    entry_id = save_fake_entry(app)
    source = app.vault.path
    original = source.read_bytes()
    config = stored_config(app)
    app.body.insert("end", "未保存的虚构修改")
    target = tmp_path / "chosen"
    target.mkdir()
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=choice), \
         patch("local_vault.app.filedialog.askdirectory", return_value=str(target)) as chooser:
        changed = app.change_location()
    if choice is None:
        assert not changed
        chooser.assert_called_once()
        assert app.vault.path == source and source.read_bytes() == original
        assert stored_config(app) == config
        assert app.dirty()
        assert not (target / "vault.lvault").exists()
        return
    assert changed and not source.exists()
    chooser.assert_called_once()
    expected = "原正文\r\n  AbC 与空格  \n" + ("未保存的虚构修改" if choice else "")
    assert app.vault.get_entry(entry_id).body == expected
    assert app.editor_entry().body == expected and not app.dirty()
    # 完整结束旧会话，再真实重开相同配置，不能仅靠当前对象的路径验证。
    app.location.close()
    restarted = LocationSession(tmp_path / "install", tmp_path / "profile")
    try:
        assert restarted.vault.path == target / "vault.lvault"
        restarted.vault.unlock(PASSWORD)
        assert restarted.vault.get_entry(entry_id).body == expected
    finally:
        restarted.close()


@pytest.mark.parametrize("dirty", [False, True])
def test_cancel_directory_selection_keeps_file_config_and_editor(app, dirty):
    save_fake_entry(app)
    source = app.vault.path
    original = source.read_bytes()
    if dirty:
        app.body.insert("end", "取消选择后保留的虚构编辑")
    editor = app.editor_entry()
    config = stored_config(app)
    with patch("local_vault.app.filedialog.askdirectory", return_value=""), \
         patch("local_vault.app.messagebox.askyesnocancel") as confirmation:
        assert not app.change_location()
    confirmation.assert_not_called()
    assert app.vault.path == source and source.read_bytes() == original
    assert stored_config(app) == config
    assert app.editor_entry() == editor and app.vault.is_unlocked
    assert app.dirty() == dirty


def test_target_conflict_is_explained_and_both_vaults_remain(app, tmp_path):
    entry_id = save_fake_entry(app)
    source = app.vault.path
    source_bytes = source.read_bytes()
    config = stored_config(app)
    target = tmp_path / "conflict"
    other = Vault(target / "vault.lvault")
    other.create("另一虚构位置密码-123-完整长口令")
    other.put_entry("另一虚构保险库", "不得覆盖的虚构内容")
    target_bytes = other.path.read_bytes()
    with patch("local_vault.app.filedialog.askdirectory", return_value=str(target)), \
         patch.object(app, "error") as error:
        assert not app.change_location()
    assert "已经存在保险库" in str(error.call_args.args[0])
    assert app.vault.path == source and source.read_bytes() == source_bytes
    assert other.path.read_bytes() == target_bytes and stored_config(app) == config
    assert app.vault.get_entry(entry_id).body == "原正文\r\n  AbC 与空格  \n"


@pytest.mark.parametrize("failure", ["directory", "config"])
def test_failed_move_returns_error_and_keeps_original_location(app, tmp_path, monkeypatch, failure):
    entry_id = save_fake_entry(app)
    source = app.vault.path
    original = source.read_bytes()
    config = stored_config(app)
    target = tmp_path / "failed"
    target.mkdir()
    if failure == "directory":
        real_probe = location_module._probe

        def deny_target(directory):
            if directory == target:
                raise PermissionError("模拟目标文件夹无写入权限")
            return real_probe(directory)

        monkeypatch.setattr(location_module, "_probe", deny_target)
    else:
        real_save = app.location._save_config

        def deny_new_setting(value):
            if value is not None and value["directory"] == str(target):
                raise PermissionError("模拟位置设置保存失败")
            return real_save(value)

        monkeypatch.setattr(app.location, "_save_config", deny_new_setting)
    with patch("local_vault.app.filedialog.askdirectory", return_value=str(target)), \
         patch.object(app, "error") as error:
        assert not app.change_location()
    assert "原保险库和原位置保留" in str(error.call_args.args[0])
    assert app.vault.path == source and source.read_bytes() == original
    assert stored_config(app) == config and not (target / "vault.lvault").exists()
    assert app.vault.get_entry(entry_id).body == "原正文\r\n  AbC 与空格  \n"


def test_idle_lock_while_choosing_location_prevents_pending_move(app, tmp_path):
    save_fake_entry(app)
    source = app.vault.path
    original = source.read_bytes()
    config = stored_config(app)
    target = tmp_path / "idle"
    target.mkdir()

    def choose_after_timeout(**options):
        # 文件夹选择期间，正文应已遮蔽；超时后待处理迁移也必须取消。
        assert app.body.get("1.0", "end-1c") == ""
        app.last_activity = time.monotonic() - 100000
        app.check_idle()
        return str(target)

    with patch("local_vault.app.filedialog.askdirectory", side_effect=choose_after_timeout), \
         patch.object(app.location, "change_directory", wraps=app.location.change_directory) as move:
        assert not app.change_location()
    move.assert_not_called()
    assert not app.vault.is_unlocked and app.body.get("1.0", "end-1c") == ""
    assert app.vault.path == source and source.read_bytes() == original
    assert stored_config(app) == config and not (target / "vault.lvault").exists()


@pytest.mark.parametrize("action", ["choose", "cancel", "idle"])
def test_actual_directory_dialog_selection_cancel_and_timeout(app, tmp_path, action):
    save_fake_entry(app)
    target = tmp_path / "dialog-folder"
    target.mkdir()
    source = app.vault.path
    original = source.read_bytes()
    dialog = VaultDirectoryDialog(app.root, title="虚构文件夹选择", initialdir=target)
    assert dialog.ok_button.cget("text") == "选择此文件夹"

    def act():
        if action == "choose":
            dialog.ok_command()
        elif action == "cancel":
            dialog.cancel_command()
        else:
            app.last_activity = time.monotonic() - 100000
            app.check_idle()

    app.root.after(30, act)
    result = dialog.show()
    assert result == (str(target) if action == "choose" else "")
    assert not dialog.top.winfo_exists() and not app.dialogs
    assert app.vault.path == source and source.read_bytes() == original
    if action == "idle":
        assert not app.vault.is_unlocked and app.body.get("1.0", "end-1c") == ""


def test_directory_dialog_rejects_nonexistent_folder_without_closing(app, tmp_path):
    dialog = VaultDirectoryDialog(app.root, title="虚构无效文件夹选择", initialdir=tmp_path)
    dialog.selection.delete(0, "end")
    dialog.selection.insert(0, str(tmp_path / "missing"))
    with patch("local_vault.app.messagebox.showwarning") as warning:
        dialog.ok_command()
    warning.assert_called_once()
    assert dialog.top.winfo_exists() and dialog.how is None
    dialog.cancel_command()


def test_directory_dialog_single_selection_uses_selected_folder(app, tmp_path):
    target = tmp_path / "single-selection"
    target.mkdir()
    dialog = VaultDirectoryDialog(app.root, title="虚构文件夹单选", initialdir=tmp_path)
    index = dialog.dirs.get(0, "end").index(target.name)
    dialog.dirs.selection_clear(0, "end")
    dialog.dirs.selection_set(index)
    dialog.dirs.activate(index)
    dialog.dirs_select_event(None)
    assert dialog.get_selection() == str(target)
    assert not dialog.files.winfo_manager() and not dialog.filesbar.winfo_manager()
    app.root.after(30, dialog.ok_command)
    assert dialog.show() == str(target)


def test_actual_directory_cancel_returns_grab_to_settings_window(app):
    save_fake_entry(app)
    source = app.vault.path
    original = source.read_bytes()
    observed = []
    errors = []

    def cancel_directory():
        directory_window = next(window for window in app.dialogs if hasattr(window, "expire"))
        directory_window.expire()

    def open_nested_directory():
        settings = next(iter(app.dialogs))
        try:
            button = next(widget for widget in widgets(settings)
                          if isinstance(widget, ttk.Button)
                          and widget.cget("text") == "更改保存位置…")
            app.root.after(30, cancel_directory)
            button.invoke()
            assert settings.winfo_exists() and settings.grab_current() == settings
            observed.append(settings)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if settings.winfo_exists():
                settings.destroy()

    app.root.after(30, open_nested_directory)
    app.settings()
    if errors:
        raise errors[0]
    assert len(observed) == 1 and not app.dialogs
    assert app.vault.path == source and source.read_bytes() == original


def test_moved_vault_keeps_save_lock_export_import_and_backup_operations(app, tmp_path):
    entry_id = save_fake_entry(app)
    source = app.vault.path
    target = tmp_path / "continued"
    target.mkdir()
    with patch("local_vault.app.filedialog.askdirectory", return_value=str(target)):
        assert app.change_location()
    app.body.insert("end", "迁移后的虚构修改\n")
    assert app.save()
    expected = "原正文\r\n  AbC 与空格  \n迁移后的虚构修改\n"
    assert not source.exists() and app.vault.path == target / "vault.lvault"
    app.manual_lock()
    assert not app.vault.is_unlocked and app.body.get("1.0", "end-1c") == ""
    with patch.object(app, "dialog_password", return_value=PASSWORD):
        assert app.ensure_unlocked()
    assert app.vault.get_entry(entry_id).body == expected

    exported = tmp_path / "out.lvexport"
    with patch.object(app, "file_dialog", return_value=str(exported)), \
         patch.object(app, "dialog_password", return_value=EXPORT_PASSWORD):
        app.export(False)
    check = Vault(tmp_path / "export-check" / "vault.lvault")
    check.create(PASSWORD)
    plan = check.inspect_import(exported, EXPORT_PASSWORD)
    check.commit_import(plan, {})
    assert check.get_entry(entry_id).body == expected

    donor = Vault(tmp_path / "donor" / "vault.lvault")
    donor.create(PASSWORD)
    incoming_id = donor.put_entry("新导入的虚构条目", "新导入的虚构正文\r\n  保留空格  ")
    incoming = tmp_path / "incoming.lvexport"
    donor.export_file(incoming, EXPORT_PASSWORD)
    accepted = []

    def accept_actual_merge():
        dialog = next(window for window in app.dialogs if isinstance(window, MergeDialog))
        accepted.append(dialog)
        dialog.accept()

    app.root.after(30, accept_actual_merge)
    with patch.object(app, "file_dialog", return_value=str(incoming)), \
         patch.object(app, "dialog_password", return_value=EXPORT_PASSWORD):
        app.import_entries()
    assert len(accepted) == 1 and not accepted[0].winfo_exists()
    assert app.vault.get_entry(entry_id).body == expected
    assert app.vault.get_entry(incoming_id).body == "新导入的虚构正文\r\n  保留空格  "

    backup = tmp_path / "complete.lvbackup"
    with patch.object(app, "file_dialog", return_value=str(backup)):
        app.backup()
    assert backup.read_bytes() == app.vault.path.read_bytes()
    app.vault.delete(incoming_id)
    with patch.object(app, "file_dialog", return_value=str(backup)), \
         patch.object(app, "dialog_password", return_value=PASSWORD), \
         patch("local_vault.app.messagebox.askyesno", return_value=True):
        app.restore()
    assert app.vault.path == target / "vault.lvault" and not source.exists()
    assert app.vault.get_entry(entry_id).body == expected
    assert app.vault.get_entry(incoming_id).body == "新导入的虚构正文\r\n  保留空格  "


@pytest.mark.parametrize("chosen", [True, False])
def test_startup_location_error_offers_directory_recovery_without_empty_vault(tmp_path, monkeypatch, chosen):
    """启动恢复只模拟窗口外围接口，真正的位置会话仍使用隔离保险库。"""
    install = tmp_path / "install"
    legacy = tmp_path / "profile" / "LocalVault"
    old = Vault(legacy / "vault.lvault")
    old.create(PASSWORD)
    entry_id = old.put_entry("虚构启动条目", "保留原库且不建立空库")
    original = old.path.read_bytes()
    conflict = Vault(install / "vault.lvault")
    conflict.create("虚构冲突密码-123-完整长口令")
    conflict_original = conflict.path.read_bytes()
    target = tmp_path / "recovery"
    target.mkdir()
    roots = []
    captured = []

    class Root:
        def __init__(self):
            self.shown = False
            roots.append(self)

        def withdraw(self):
            pass

        def deiconify(self):
            self.shown = True

        def mainloop(self):
            pass

    def capture_app(root, vault, session):
        assert vault.is_initialized
        vault.unlock(PASSWORD)
        captured.append((vault.path, vault.get_entry(entry_id).body))

    monkeypatch.setattr(app_module.tk, "Tk", Root)
    monkeypatch.setattr(app_module, "VaultApp", capture_app)
    monkeypatch.setattr(app_module, "installation_directory", lambda: install)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    monkeypatch.setattr(app_module.sys, "argv", ["LocalVault.exe"])
    with patch("local_vault.app.messagebox.askyesno", return_value=True) as confirmation, \
         patch("local_vault.app.filedialog.askdirectory", return_value=str(target) if chosen else "") as chooser, \
         patch("local_vault.app.messagebox.showerror") as error:
        app_module.main()
    confirmation.assert_called_once()
    chooser.assert_called_once()
    error.assert_not_called()
    assert "原保险库会保留" in confirmation.call_args.args[1]
    assert conflict.path.read_bytes() == conflict_original
    if chosen:
        assert captured == [(target / "vault.lvault", "保留原库且不建立空库")]
        assert (target / "vault.lvault").read_bytes() == original
        assert not old.path.exists() and roots[0].shown
    else:
        assert not captured and not roots[0].shown
        assert old.path.read_bytes() == original and not (target / "vault.lvault").exists()
