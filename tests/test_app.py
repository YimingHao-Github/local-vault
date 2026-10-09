"""使用真正的桌面组件与虚构内容检查界面行为。"""
import os
import time
import tkinter as tk
import ctypes
from ctypes import wintypes
from unittest.mock import patch

import pytest

from local_vault.app import InstanceLock, MergeDialog, MessageDialog, PasswordDialog, VaultApp, VaultFileDialog
from local_vault.core import Entry, Vault, VaultError


def system_clipboard_text():
    """直接检查 Windows 系统剪贴板，避开 Tk 读取长中文的分片缺陷。"""
    if os.name != "nt":
        pytest.skip("此验证针对 Windows 系统剪贴板")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    assert user32.OpenClipboard(None), "测试无法打开系统剪贴板"
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        assert handle, "系统剪贴板缺少 Unicode 纯文本"
        pointer = kernel32.GlobalLock(handle)
        assert pointer, "测试无法读取系统剪贴板文本"
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


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
    # 正式应用仅有一个 Tcl/Tk 解释器；各测试在独立窗口中隔离界面。
    root = tk.Toplevel(tk_host)
    root.withdraw()
    previous_callbacks = set(root.tk.call("after", "info"))
    vault = Vault(tmp_path / "vault.lvault")
    vault.create("虚构测试主密码-123-完整长口令")
    application = VaultApp(root, vault)
    yield application
    for callback in set(root.tk.call("after", "info")) - previous_callbacks:
        root.after_cancel(callback)
    for event in ("<KeyPress>", "<ButtonPress>", "<MouseWheel>"):
        root.unbind_all(event)
    root.destroy()


def test_plain_text_widget_preserves_text_and_save(app):
    body = "中文\r\n  大小写 AbC  \n\t配置 = 假值\n\n" + "很长的虚构说明\n" * 2000
    app.set_editor(Entry("", "虚构标题", body))
    app.original = None
    assert app.editor_entry().body == body
    assert app.dirty()
    assert app.save()
    assert not app.dirty()
    assert app.vault.get_entry(app.current_id).body == body
    app.body.insert("end", "新增内容")
    assert app.dirty()
    app.select_all()
    assert app.body.get("sel.first", "sel.last") == body + "新增内容"
    app.body.event_generate("<<Copy>>")
    app.root.update()
    assert system_clipboard_text() == (body + "新增内容").replace("\n", "\r\n")
    app.body.delete("1.0", "end")
    app.body.event_generate("<<Paste>>")
    app.root.update()
    assert app.editor_entry().body == body + "新增内容"
    app.root.clipboard_clear()  # 只清除虚构测试内容，产品没有自动清除功能。


def test_unsaved_cancel_discard_and_save(app):
    entry_id = app.vault.put_entry("标题", "保存版本")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "修改")
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=None):
        assert not app.resolve_dirty()
        assert app.dirty()
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=False):
        assert app.resolve_dirty()
        assert not app.dirty()
        assert app.editor_entry().body == "保存版本"
    app.body.insert("end", "修改")
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=True):
        assert app.resolve_dirty()
        assert app.vault.get_entry(entry_id).body == "保存版本修改"


def test_idle_lock_clears_widget_and_recovers_encrypted_unsaved_draft(app):
    entry_id = app.vault.put_entry("标题", "原文")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "未保存的中文秘密")
    app.last_activity = time.monotonic() - 100000
    app.idle_tick()
    assert not app.vault.is_unlocked
    assert app.body.get("1.0", "end-1c") == ""
    assert app.draft is not None
    assert "未保存的中文秘密".encode() not in app.draft
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    assert app.editor_entry().body == "原文未保存的中文秘密"
    assert app.dirty()
    assert app.vault.get_entry(entry_id).body == "原文"
    assert app.save()


def test_title_search_and_checkbox_without_unlock(app):
    first = app.vault.put_entry("甲中文条目", "虚构正文")
    second = app.vault.put_entry("乙条目", "另一虚构正文")
    app.manual_lock()
    app.refresh_list()
    app.search.set("中文")
    assert app.tree.get_children() == (first,)
    app.check_visible()
    assert app.checked == {first}
    app.search.set("")
    app.toggle_check(second)
    assert app.checked == {first, second}
    assert not app.vault.is_unlocked
    app.clear_checked()
    assert app.checked == set()


def test_close_cancel_keeps_application_alive(app):
    app.set_editor(Entry("", "新标题", "未保存"))
    app.original = None
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=None):
        app.close()
    assert app.root.winfo_exists()


def test_single_instance_lock_released(tmp_path):
    first = InstanceLock(tmp_path)
    try:
        with pytest.raises(VaultError):
            InstanceLock(tmp_path)
    finally:
        first.close()
    second = InstanceLock(tmp_path)
    second.close()


def test_switch_entry_after_saving_dirty_editor_selects_requested_entry(app):
    first = app.vault.put_entry("虚构条目甲", "甲的保存正文")
    second = app.vault.put_entry("虚构条目乙", "乙的保存正文")
    app.set_editor(app.vault.get_entry(first))
    app.refresh_list()
    app.root.update()
    app.body.insert("end", "甲的未保存修改")
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=True):
        app.tree.selection_set(second)
        app.select_entry()
        app.root.update()
    assert app.current_id == second
    assert app.tree.selection() == (second,)
    assert app.editor_entry().body == "乙的保存正文"
    assert app.vault.get_entry(first).body == "甲的保存正文甲的未保存修改"


def test_idle_draft_saved_before_switching_to_other_entry(app):
    first = app.vault.put_entry("虚构条目甲", "甲原文")
    second = app.vault.put_entry("虚构条目乙", "乙原文")
    app.set_editor(app.vault.get_entry(first))
    app.refresh_list()
    app.root.update()
    app.body.insert("end", "  甲未保存\r\nAbC  ")
    expected = app.editor_entry().body
    app.last_activity = time.monotonic() - 100000
    app.idle_tick()
    assert not app.vault.is_unlocked
    assert app.draft is not None
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        with patch("local_vault.app.messagebox.askyesnocancel", return_value=True):
            app.tree.selection_set(second)
            app.select_entry()
            app.root.update()
    assert app.vault.get_entry(first).body == expected
    assert app.current_id == second
    assert app.tree.selection() == (second,)
    assert app.editor_entry().body == "乙原文"
    assert app.draft is None


def test_idle_lock_during_unsaved_confirmation_preserves_protected_draft(app):
    entry_id = app.vault.put_entry("虚构条目", "原正文")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "未保存修改")
    original = app.editor_entry()
    app.last_activity = time.monotonic() - 100000

    def simulated_modal_confirmation(*args, **kwargs):
        assert app.modal_depth > 0
        app.idle_tick()
        assert not app.vault.is_unlocked
        assert app.draft is not None
        assert app.body.get("1.0", "end-1c") == ""
        return True

    with patch("local_vault.app.messagebox.askyesnocancel", side_effect=simulated_modal_confirmation):
        assert not app.resolve_dirty()
    assert app.modal_depth == 0
    assert time.monotonic() - app.last_activity > 100
    assert not app.vault.is_unlocked
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    assert app.editor_entry() == original


def prepare_locked_exit_draft(app, new=False, title="虚构退出草稿标题"):
    """制造独立保险库中的虚构草稿，再通过真实闲置逻辑锁定。"""
    if new:
        app.set_editor(Entry("", title, ""))
        app.original = None
    else:
        entry_id = app.vault.put_entry("虚构保存标题", "已保存正文\r\n  AbC  \n")
        app.set_editor(app.vault.get_entry(entry_id))
        app.title.set(title)
    app.body.insert("end", "\t未保存中文修改\r\n尾部空格  ")
    expected = app.editor_entry()
    original = app.vault.path.read_bytes()
    app.last_activity = time.monotonic() - 100000
    app.idle_tick()
    assert not app.vault.is_unlocked and app.draft is not None
    assert app.body.get("1.0", "end-1c") == ""
    return expected, original, app.draft


def assert_exit_saved_expected(app, expected):
    reopened = Vault(app.vault.path)
    reopened.unlock("虚构测试主密码-123-完整长口令")
    titles = reopened.list_titles()
    assert len(titles) == 1
    entry = reopened.get_entry(expected.id or titles[0][0])
    assert (entry.title, entry.body) == (expected.title, expected.body)


@pytest.mark.parametrize("new", [False, True])
def test_locked_draft_discard_exit_without_password(app, new):
    expected, original, _ = prepare_locked_exit_draft(app, new)
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=False), \
         patch.object(app, "dialog_password", return_value=None) as password, \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    password.assert_not_called()
    destroy.assert_called_once_with()
    assert app.draft is None
    assert not app.vault.is_unlocked
    assert app.vault.path.read_bytes() == original
    reopened = Vault(app.vault.path)
    reopened.unlock("虚构测试主密码-123-完整长口令")
    if new:
        assert reopened.list_titles() == []
    else:
        assert reopened.get_entry(expected.id).body == "已保存正文\r\n  AbC  \n"


@pytest.mark.parametrize("new", [False, True])
def test_locked_draft_cancel_exit_preserves_locked_draft(app, new):
    expected, original, sealed = prepare_locked_exit_draft(app, new)
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=None), \
         patch.object(app, "dialog_password", return_value=None) as password, \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    password.assert_not_called()
    destroy.assert_not_called()
    assert app.root.winfo_exists()
    assert not app.vault.is_unlocked and app.draft == sealed
    assert app.vault.path.read_bytes() == original
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    assert app.editor_entry() == expected
    assert app.dirty()


@pytest.mark.parametrize("new", [False, True])
def test_locked_draft_save_exit_unlocks_and_persists(app, new):
    expected, _, _ = prepare_locked_exit_draft(app, new)
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=True), \
         patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令") as password, \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    password.assert_called_once()
    destroy.assert_called_once_with()
    assert app.draft is None and not app.vault.is_unlocked
    assert_exit_saved_expected(app, expected)


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("failure", ["cancel_password", "wrong_password", "replace"])
def test_locked_draft_failed_save_exit_preserves_draft_and_retries(app, monkeypatch, new, failure):
    expected, original, sealed = prepare_locked_exit_draft(app, new)
    real_replace = os.replace

    def fail_final(source, destination):
        if failure == "replace" and os.path.normcase(str(destination)) == os.path.normcase(str(app.vault.path)):
            raise PermissionError("模拟保存退出的最终替换失败")
        return real_replace(source, destination)

    password = None if failure == "cancel_password" else (
        "虚构错误主密码-123" if failure == "wrong_password" else "虚构测试主密码-123-完整长口令")
    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail_final)
        with patch("local_vault.app.messagebox.askyesnocancel", return_value=True), \
             patch.object(app, "dialog_password", return_value=password), \
             patch.object(app, "error"), \
             patch.object(app.root, "destroy") as destroy:
            app.close()
        destroy.assert_not_called()
    assert app.root.winfo_exists()
    assert not app.vault.is_unlocked and app.draft == sealed
    assert app.body.get("1.0", "end-1c") == ""
    assert app.vault.path.read_bytes() == original
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=True), \
         patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"), \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    destroy.assert_called_once_with()
    assert app.draft is None and not app.vault.is_unlocked
    assert_exit_saved_expected(app, expected)


def test_locked_draft_invalid_title_can_discard_after_failed_save(app):
    _, original, sealed = prepare_locked_exit_draft(app, new=True, title="   ")
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=True), \
         patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"), \
         patch("local_vault.app.messagebox.showwarning"), \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    destroy.assert_not_called()
    assert not app.vault.is_unlocked and app.draft == sealed
    assert app.vault.path.read_bytes() == original
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=False), \
         patch.object(app, "dialog_password") as password, \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    password.assert_not_called()
    destroy.assert_called_once_with()
    assert app.vault.path.read_bytes() == original


@pytest.mark.parametrize("choice", [None, False, True])
def test_unlocked_dirty_close_keeps_original_choices(app, choice):
    entry_id = app.vault.put_entry("虚构未锁定标题", "保存版本")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "未保存修改")
    original = app.vault.path.read_bytes()
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=choice), \
         patch.object(app, "dialog_password") as password, \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    password.assert_not_called()
    if choice is None:
        destroy.assert_not_called()
        assert app.vault.is_unlocked and app.dirty()
        assert app.editor_entry().body == "保存版本未保存修改"
    else:
        destroy.assert_called_once_with()
        assert not app.vault.is_unlocked
    reopened = Vault(app.vault.path)
    reopened.unlock("虚构测试主密码-123-完整长口令")
    assert reopened.get_entry(entry_id).body == ("保存版本未保存修改" if choice else "保存版本")
    if not choice:
        assert app.vault.path.read_bytes() == original


@pytest.mark.parametrize("body", [
    "虚构中文正文：甲乙丙，AbC123。",
    "  前后空格  \n\t虚构配置 = 假值\n\n末行空格  \n",
    "中文首行\r\n  AbC 大小写  \n\t混合换行\r\n",
    "很长的虚构中文说明\n  空格与 AbC 保留\r\n" * 2000,
], ids=["chinese", "whitespace", "mixed-newlines", "long-text"])
def test_loaded_body_undo_preserves_saved_text(app, body):
    entry_id = app.vault.put_entry("虚构撤销条目", body)
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.editor_entry().body == body
    assert not app.dirty()
    assert app.vault.get_entry(entry_id).body == body


def test_loaded_body_user_edit_undo_and_redo(app):
    body = "虚构原文\r\n  保留空格 AbC  \n\t配置 = 假值\n"
    change = "  新增中文修改\r\n"
    entry_id = app.vault.put_entry("虚构编辑撤销条目", body)
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", change)
    app.body.edit_separator()
    assert app.editor_entry().body == body + change
    assert app.dirty()
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.editor_entry().body == body
    assert not app.dirty()
    app.body.event_generate("<<Redo>>")
    app.root.update()
    assert app.editor_entry().body == body + change
    assert app.dirty()
    assert app.vault.get_entry(entry_id).body == body


def test_switch_entry_resets_loaded_body_undo_history(app):
    first = app.vault.put_entry("虚构撤销条目甲", "甲的正文\n")
    second_body = "乙的正文\r\n  空格 AbC  \n"
    second = app.vault.put_entry("虚构撤销条目乙", second_body)
    app.set_editor(app.vault.get_entry(first))
    app.refresh_list()
    app.root.update()
    app.body.insert("end", "甲的未保存修改")
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=False):
        app.tree.selection_set(second)
        app.select_entry()
        app.root.update()
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.current_id == second
    assert app.editor_entry().body == second_body
    assert not app.dirty()
    app.body.insert("end", "乙的新增修改")
    app.body.edit_separator()
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.editor_entry().body == second_body
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.editor_entry().body == second_body


def test_unlocked_draft_resets_loaded_body_undo_history(app):
    saved_body = "虚构已保存正文\r\n  AbC  \n"
    entry_id = app.vault.put_entry("虚构草稿撤销条目", saved_body)
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "\t未保存中文草稿\n" * 1000)
    draft_body = app.editor_entry().body
    app.last_activity = time.monotonic() - 100000
    app.idle_tick()
    assert not app.vault.is_unlocked
    assert app.draft is not None
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.editor_entry().body == draft_body
    assert app.dirty()
    app.body.insert("end", "恢复后的新增修改")
    app.body.edit_separator()
    app.body.event_generate("<<Undo>>")
    app.root.update()
    assert app.editor_entry().body == draft_body
    app.body.event_generate("<<Redo>>")
    app.root.update()
    assert app.editor_entry().body == draft_body + "恢复后的新增修改"
    assert app.vault.get_entry(entry_id).body == saved_body


@pytest.mark.parametrize("stage", ["file", "password", "confirmation"])
def test_cancel_restore_preserves_vault_and_backup(app, tmp_path, stage):
    entry_id = app.vault.put_entry("虚构恢复取消条目", "必须保留的已保存正文")
    app.vault.put_entry("虚构恢复取消条目", "当前已保存正文", entry_id=entry_id)
    backup = tmp_path / "cancel.lvbackup"
    source = Vault(tmp_path / "restore-source.lvault")
    source.create("虚构恢复主密码-123-完整长口令")
    source.put_entry("虚构备份条目", "恢复后会替换当前内容")
    source.backup_file(backup)
    app.set_editor(app.vault.get_entry(entry_id))
    editor_before = app.editor_entry()
    automatic = app.vault.path.with_name(app.vault.path.name + ".bak")
    automatic.write_bytes(b"FAKE-LEGACY-FILE-ONLY")  # 新版保留旧版遗留用户文件。
    before = {file: file.read_bytes() for file in (app.vault.path, automatic, backup)}
    with patch("local_vault.app.filedialog.askopenfilename",
               return_value="" if stage == "file" else str(backup)), \
         patch.object(app, "dialog_password",
                      return_value=None if stage == "password" else "虚构恢复主密码-123-完整长口令") as password, \
         patch("local_vault.app.messagebox.askyesno", return_value=False) as confirmation, \
         patch.object(app.vault, "restore_backup") as restore:
        app.restore()
    restore.assert_not_called()
    if stage == "file":
        password.assert_not_called()
    if stage in ("file", "password"):
        confirmation.assert_not_called()
    assert {file: file.read_bytes() for file in before} == before
    assert app.vault.get_entry(entry_id).body == "当前已保存正文"
    assert app.editor_entry() == editor_before
    assert app.vault.is_unlocked


@pytest.mark.parametrize("kind", ["settings", "password", "message", "file"])
def test_actual_dialog_idle_timeout_closes_window_and_hides_body(app, tmp_path, kind):
    from local_vault.app import filedialog, messagebox

    entry_id = app.vault.put_entry("虚构对话条目", "虚构敏感正文")
    app.set_editor(app.vault.get_entry(entry_id))
    windows = []

    def expire():
        windows.extend(app.dialogs)
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()

    app.root.after(30, expire)
    if kind == "settings":
        app.settings()
    elif kind == "password":
        assert app.dialog_password("虚构密码窗口", "请输入虚构密码", True) is None
    elif kind == "message":
        assert messagebox.askyesnocancel("虚构确认", "保存、放弃或取消", parent=app.root) is None
    else:
        assert app.file_dialog(filedialog.askopenfilename, title="虚构文件选择", initialdir=tmp_path) == ""
    assert windows
    assert all(not window.winfo_exists() for window in windows)
    assert not app.vault.is_unlocked
    assert app.body.get("1.0", "end-1c") == ""
    assert not app.dialogs


def test_actual_dialog_input_refreshes_idle_timer(app):
    observed = []

    def interact():
        window = next(iter(app.dialogs))
        app.last_activity = time.monotonic() - 100000
        window.event_generate("<ButtonPress-1>", x=1, y=1)
        app.check_idle()
        observed.append(app.vault.is_unlocked)
        window.destroy()

    app.root.after(30, interact)
    app.settings()
    assert observed == [True]
    assert time.monotonic() - app.last_activity < 5


def test_actual_merge_preview_timeout_clears_preview_and_invalidates_plan(app, tmp_path):
    source = Vault(tmp_path / "merge-source.lvault")
    source.create("虚构导入源主密码-123-长口令")
    source.put_entry("虚构待导入条目", "虚构导入敏感正文")
    export = tmp_path / "merge.lvexport"
    source.export_file(export, "虚构导出文件密码-123-长口令")
    plan = app.vault.inspect_import(export, "虚构导出文件密码-123-长口令")
    observed = []

    def expire():
        dialog = next(window for window in app.dialogs if isinstance(window, MergeDialog))
        observed.append(dialog)
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()

    app.root.after(30, expire)
    dialog = MergeDialog(app.root, plan)
    assert observed == [dialog]
    assert dialog.result is None and dialog.plan is None and not dialog.choices
    assert not dialog.winfo_exists()
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    with pytest.raises(VaultError):
        app.vault.commit_import(plan, {})


def test_pending_import_cannot_commit_after_lock_and_reunlock(app, tmp_path):
    source = Vault(tmp_path / "pending-source.lvault")
    source.create("虚构待处理源密码-123-长口令")
    source.put_entry("虚构待处理条目", "虚构待处理正文")
    export = tmp_path / "pending.lvexport"
    source.export_file(export, "虚构待处理导出密码-123-长口令")
    original = app.vault.path.read_bytes()

    def stale_dialog(*args):
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()
        assert app.ensure_unlocked()
        result = type("ExpiredResult", (), {})()
        result.result = {}
        return result

    with patch("local_vault.app.filedialog.askopenfilename", return_value=str(export)), \
         patch.object(app, "dialog_password", side_effect=["虚构待处理导出密码-123-长口令", "虚构测试主密码-123-完整长口令"]), \
         patch("local_vault.app.MergeDialog", side_effect=stale_dialog), \
         patch.object(app.vault, "commit_import") as commit:
        app.import_entries()
    commit.assert_not_called()
    assert app.vault.path.read_bytes() == original
    assert "重新开始" in app.status.get()


def test_file_selection_hides_body_and_expired_operation_cancels(app, tmp_path):
    entry_id = app.vault.put_entry("虚构选择文件条目", "选择期间不能显示的虚构正文")
    app.set_editor(app.vault.get_entry(entry_id))
    original = app.vault.path.read_bytes()

    def pick(**options):
        assert app.body.get("1.0", "end-1c") == ""
        app.last_activity = time.monotonic() - 100000
        return str(tmp_path / "unused.lvexport")

    with patch("local_vault.app.filedialog.asksaveasfilename", side_effect=pick), \
         patch.object(app, "dialog_password") as password, \
         patch.object(app.vault, "export_file") as export:
        app.export(False)
    password.assert_not_called()
    export.assert_not_called()
    assert not app.vault.is_unlocked
    assert app.body.get("1.0", "end-1c") == ""
    assert app.vault.path.read_bytes() == original


def test_save_in_progress_completes_before_idle_lock(app):
    entry_id = app.vault.put_entry("虚构写入条目", "原正文")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "保存后的虚构修改")
    expected = app.editor_entry().body
    put = app.vault.put_entry
    observed = []

    def put_and_expire(*args):
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()
        observed.append(app.vault.is_unlocked)
        return put(*args)

    with patch.object(app.vault, "put_entry", side_effect=put_and_expire):
        assert app.save()
    assert observed == [True]
    assert not app.vault.is_unlocked and app.draft is None
    reopened = Vault(app.vault.path)
    reopened.unlock("虚构测试主密码-123-完整长口令")
    assert reopened.get_entry(entry_id).body == expected


@pytest.mark.parametrize("failure", [VaultError("虚构草稿错误"), OSError("虚构加密失败"), RuntimeError("虚构加密异常")])
def test_draft_encryption_failure_hides_body_and_requires_password_to_recover(app, failure):
    entry_id = app.vault.put_entry("虚构异常草稿", "已保存的虚构正文")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "绝不能丢失的虚构未保存编辑")
    expected = app.editor_entry()
    original = app.vault.path.read_bytes()

    def reported(*args, **kwargs):
        assert not app.vault.is_unlocked
        assert app.body.get("1.0", "end-1c") == ""
        assert "尚未加密" in app.lock_label.cget("text")

    with patch.object(app.vault, "seal_draft", side_effect=failure), \
         patch("local_vault.app.messagebox.showerror", side_effect=reported):
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()
    assert app.blocked_draft == expected and app.draft is None
    assert app.dirty()
    assert app.vault.path.read_bytes() == original
    with patch.object(app, "dialog_password", return_value="虚构错误密码-123"), patch.object(app, "error"):
        assert not app.ensure_unlocked()
    assert app.blocked_draft == expected and not app.vault.is_unlocked
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    assert app.blocked_draft is None and app.editor_entry() == expected
    assert app.save()
    assert app.vault.get_entry(entry_id).body == expected.body


@pytest.mark.parametrize("choice", [None, False, True])
def test_failed_encryption_draft_exit_has_explicit_save_discard_cancel(app, choice):
    app.set_editor(Entry("", "虚构异常退出草稿", "虚构未保存正文"))
    app.original = None
    expected = app.editor_entry()
    original = app.vault.path.read_bytes()
    with patch.object(app.vault, "seal_draft", side_effect=VaultError("虚构草稿加密失败")), \
         patch("local_vault.app.messagebox.showerror"):
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=choice), \
         patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令") as password, \
         patch.object(app.root, "destroy") as destroy:
        app.close()
    if choice is None:
        password.assert_not_called()
        destroy.assert_not_called()
        assert app.blocked_draft == expected and not app.vault.is_unlocked
        assert app.vault.path.read_bytes() == original
    elif choice is False:
        password.assert_not_called()
        destroy.assert_called_once()
        assert app.blocked_draft is None and app.vault.path.read_bytes() == original
    else:
        password.assert_called_once()
        destroy.assert_called_once()
        assert app.blocked_draft is None
        assert_exit_saved_expected(app, expected)


@pytest.mark.parametrize("failure", ["wrong_password", "save_failure"])
def test_failed_encryption_draft_failed_exit_keeps_hidden_edit_for_retry(app, failure):
    app.set_editor(Entry("", "虚构失败退出草稿", "不能丢失的虚构正文"))
    app.original = None
    expected = app.editor_entry()
    original = app.vault.path.read_bytes()
    with patch.object(app.vault, "seal_draft", side_effect=VaultError("虚构加密失败")), \
         patch("local_vault.app.messagebox.showerror"):
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()
    password = "虚构错误密码-123" if failure == "wrong_password" else "虚构测试主密码-123-完整长口令"
    with patch("local_vault.app.messagebox.askyesnocancel", return_value=True), \
         patch.object(app, "dialog_password", return_value=password), \
         patch.object(app.vault, "put_entry", side_effect=OSError("虚构保存失败")), \
         patch.object(app, "error"), patch.object(app.root, "destroy") as destroy:
        app.close()
    destroy.assert_not_called()
    assert not app.vault.is_unlocked and app.blocked_draft == expected
    assert app.body.get("1.0", "end-1c") == ""
    assert app.vault.path.read_bytes() == original
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123-完整长口令"):
        assert app.ensure_unlocked()
    assert app.editor_entry() == expected


@pytest.mark.parametrize("kind", ["password_warning", "file_overwrite"])
def test_nested_dialogs_expire_together_without_restoring_invalid_grab(app, tmp_path, kind):
    entry_id = app.vault.put_entry("虚构嵌套对话条目", "超时后必须遮蔽的虚构正文")
    app.set_editor(app.vault.get_entry(entry_id))
    expired = []

    def expire_nested():
        expired.extend(app.dialogs)
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()

    if kind == "password_warning":
        def trigger():
            dialog = next(window for window in app.dialogs if isinstance(window, PasswordDialog))
            dialog.password.set("too-short")
            dialog.confirmation.set("too-short")
            app.root.after(30, expire_nested)
            dialog.accept(True)

        app.root.after(30, trigger)
        assert app.dialog_password("虚构新密码", "验证弱密码提示期间锁定", True) is None
    else:
        existing = tmp_path / "overwrite.lvexport"
        existing.write_text("FAKE-FILE-ONLY", encoding="utf-8")
        dialog = VaultFileDialog(app.root, save=True, title="虚构覆盖确认", initialdir=tmp_path,
                                 initialfile=existing.name, defaultextension=".lvexport")

        def trigger():
            app.root.after(30, expire_nested)
            dialog.ok_command()

        app.root.after(30, trigger)
        assert dialog.show() == ""
        assert existing.read_text(encoding="utf-8") == "FAKE-FILE-ONLY"
    assert len(expired) == 2
    assert all(not window.winfo_exists() for window in expired)
    assert not app.dialogs and app.root.grab_current() is None
    assert not app.vault.is_unlocked
    assert app.body.get("1.0", "end-1c") == ""


def test_new_password_dialog_rejects_short_password_and_keeps_exact_long_phrase(app):
    expected = "  虚构保留全部空格的很长主口令  "
    rejected = []

    def enter():
        dialog = next(window for window in app.dialogs if isinstance(window, PasswordDialog))
        dialog.password.set("12345678")
        dialog.confirmation.set("12345678")
        with patch("local_vault.app.messagebox.showerror", side_effect=lambda *a, **k: rejected.append(a)):
            dialog.accept(True)
        assert dialog.winfo_exists()
        dialog.password.set(expected)
        dialog.confirmation.set(expected)
        dialog.accept(True)

    app.root.after(30, enter)
    assert app.dialog_password("虚构设置密码", "输入长口令", True) == expected
    assert len(rejected) == 1


def test_expired_delete_confirmation_cannot_resume_old_action(app):
    entry_id = app.vault.put_entry("虚构删除确认", "保留原正文")
    app.set_editor(app.vault.get_entry(entry_id))
    original = app.vault.path.read_bytes()

    def stale_confirmation(*args, **kwargs):
        app.last_activity = time.monotonic() - 100000
        app.idle_tick()
        return True

    with patch("local_vault.app.messagebox.askyesno", side_effect=stale_confirmation), \
         patch.object(app, "dialog_password") as password, \
         patch.object(app.vault, "delete") as delete:
        app.delete_entry()
    password.assert_not_called()
    delete.assert_not_called()
    assert app.vault.path.read_bytes() == original and not app.vault.is_unlocked
