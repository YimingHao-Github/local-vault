"""使用真正的桌面组件与虚构内容检查界面行为。"""
import os
import time
import tkinter as tk
import ctypes
from ctypes import wintypes
from unittest.mock import patch

import pytest

from local_vault.app import InstanceLock, MergeDialog, VaultApp
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
    vault.create("虚构测试主密码-123")
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
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123"):
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
    with patch.object(app, "dialog_password", return_value="虚构测试主密码-123"):
        with patch("local_vault.app.messagebox.askyesnocancel", return_value=True):
            app.tree.selection_set(second)
            app.select_entry()
            app.root.update()
    assert app.vault.get_entry(first).body == expected
    assert app.current_id == second
    assert app.tree.selection() == (second,)
    assert app.editor_entry().body == "乙原文"
    assert app.draft is None


def test_idle_lock_paused_during_unsaved_confirmation(app):
    entry_id = app.vault.put_entry("虚构条目", "原正文")
    app.set_editor(app.vault.get_entry(entry_id))
    app.body.insert("end", "未保存修改")
    original = app.editor_entry()
    app.last_activity = time.monotonic() - 100000

    def simulated_modal_confirmation(*args, **kwargs):
        assert app.modal_depth > 0
        app.idle_tick()
        assert app.vault.is_unlocked
        assert app.editor_entry() == original
        return None

    with patch("local_vault.app.messagebox.askyesnocancel", side_effect=simulated_modal_confirmation):
        assert not app.resolve_dirty()
    assert app.modal_depth == 0
    assert time.monotonic() - app.last_activity < 5
    assert app.vault.is_unlocked
    assert app.editor_entry() == original
