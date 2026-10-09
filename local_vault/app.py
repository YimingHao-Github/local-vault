"""本地密匣的中文桌面界面。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from tkinter import filedialog as tk_filedialog, messagebox as tk_messagebox, ttk

from local_vault import __version__
from local_vault.core import Entry, Vault, VaultError, validate_new_password


def dialog_app(parent):
    while parent is not None:
        app = getattr(parent, "vault_app", None)
        if app is not None:
            return app
        parent = getattr(parent, "master", None)
    return None


class MessageDialog(tk.Toplevel):
    """保持常用确认流程，同时允许 Tk 闲置回调正常运行。"""
    def __init__(self, parent, title, text, choices):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        frame = ttk.Frame(self, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=text, wraplength=460).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(18, 0))
        for label, value in choices:
            ttk.Button(buttons, text=label, command=lambda v=value: self.accept(v)).pack(side="left", padx=5)
        self.protocol("WM_DELETE_WINDOW", self.expire)
        self.bind("<Escape>", lambda e: self.expire())
        self.app = dialog_app(parent)
        if self.app is not None:
            self.app.register_dialog(self)
        previous_grab = parent.grab_current()
        self.grab_set()
        try:
            parent.wait_window(self)
        finally:
            if self.app is not None:
                self.app.unregister_dialog(self)
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()

    def accept(self, value):
        self.result = value
        self.destroy()

    def expire(self):
        self.result = None
        self.destroy()


class MessageBoxes:
    def _show(self, kind, title, text, parent=None, **options):
        if dialog_app(parent) is None:
            return getattr(tk_messagebox, kind)(title, text, parent=parent, **options)
        choices = [("是", True), ("否", False)] if kind.startswith("ask") else [("确定", "ok")]
        if kind == "askyesnocancel":
            choices.append(("取消", None))
        return MessageDialog(parent, title, text, choices).result

    def askyesno(self, title, text, **options):
        return self._show("askyesno", title, text, **options)

    def askyesnocancel(self, title, text, **options):
        return self._show("askyesnocancel", title, text, **options)

    def showerror(self, title, text, **options):
        return self._show("showerror", title, text, **options)

    def showwarning(self, title, text, **options):
        return self._show("showwarning", title, text, **options)

    def showinfo(self, title, text, **options):
        return self._show("showinfo", title, text, **options)


messagebox = MessageBoxes()


class VaultFileDialog(tk_filedialog.FileDialog):
    """使用同一事件循环选择文件，避免原生窗口无限拖延锁定。"""
    def __init__(self, parent, *, save=False, **options):
        super().__init__(parent, options.get("title", "选择文件"))
        self.top.transient(parent)
        self.top.geometry("760x460")
        self.ok_button.configure(text="保存" if save else "打开")
        self.filter_button.configure(text="刷新目录")
        self.cancel_button.configure(text="取消")
        self.top.bind("<Escape>", self.cancel_command)
        self.save = save
        self.extension = options.get("defaultextension", "")
        self.app = dialog_app(parent)
        self.epoch = self.app.session_epoch if self.app is not None else None
        self.how = None
        self.directory = str(options.get("initialdir", Path.home()))
        pattern = options.get("filetypes", [("所有文件", "*")])[0][1]
        self.set_filter(self.directory, pattern)
        self.set_selection(options.get("initialfile", ""))
        self.filter_command()
        # 文件窗口只有路径信息；超时取消时也退出等待，不阻塞主窗口。
        self.top.expire = self.cancel_command

    def show(self):
        self.selection.focus_set()
        self.top.grab_set()
        if self.app is not None:
            self.app.register_dialog(self.top)
        try:
            self.master.wait_window(self.top)
            return self.how or ""
        finally:
            if self.app is not None:
                self.app.unregister_dialog(self.top)

    def quit(self, how=None):
        self.how = how
        self.top.destroy()

    def ok_command(self):
        filename = Path(self.get_selection()).expanduser()
        if self.save and self.extension and not filename.suffix:
            filename = filename.with_suffix(self.extension)
        if self.app is not None and not self.app.continue_session(self.epoch, require_unlocked=False):
            return
        if self.save:
            if filename.is_dir() or not filename.parent.is_dir():
                messagebox.showwarning("路径无效", "请选择有效的文件保存位置。", parent=self.top)
                return
            if filename.exists():
                if not messagebox.askyesno("覆盖文件", f"文件已存在，是否覆盖？\n{filename}", parent=self.top):
                    return
                if not self.top.winfo_exists():
                    return
        elif not filename.is_file():
            messagebox.showwarning("文件不存在", "请选择一个已存在的文件。", parent=self.top)
            return
        self.quit(str(filename))


class FileDialogs:
    def askopenfilename(self, parent=None, **options):
        if dialog_app(parent) is None:
            return tk_filedialog.askopenfilename(parent=parent, **options)
        return VaultFileDialog(parent, **options).show()

    def asksaveasfilename(self, parent=None, **options):
        if dialog_app(parent) is None:
            return tk_filedialog.asksaveasfilename(parent=parent, **options)
        return VaultFileDialog(parent, save=True, **options).show()


filedialog = FileDialogs()


def interaction(method):
    """对话流程仍检查闲置；只有实际键鼠操作刷新计时。"""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self.modal_depth += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self.modal_depth -= 1
            self.check_idle()
    return wrapped


class PasswordDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, confirm=False):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        frame = ttk.Frame(self, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt, wraplength=380).pack(anchor="w", pady=(0, 12))
        self.password = tk.StringVar()
        first = ttk.Entry(frame, textvariable=self.password, show="●", width=42)
        first.pack(fill="x")
        self.confirmation = tk.StringVar()
        if confirm:
            ttk.Label(frame, text="再次输入密码").pack(anchor="w", pady=(12, 4))
            ttk.Entry(frame, textvariable=self.confirmation, show="●").pack(fill="x")
            ttk.Label(frame, text="16～1024 个字符，建议使用较长口令，避免常见弱密码；忘记后无法恢复正文。",
                      wraplength=380).pack(anchor="w", pady=(10, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="确定", command=lambda: self.accept(confirm)).pack(side="right", padx=8)
        self.bind("<Return>", lambda e: self.accept(confirm))
        self.bind("<Escape>", lambda e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx()+100}+{parent.winfo_rooty()+100}")
        self.deiconify()
        first.focus_set()
        self.grab_set()
        app = getattr(parent, "vault_app", None)
        if app is not None:
            app.register_dialog(self)
        try:
            parent.wait_window(self)
        finally:
            if app is not None:
                app.unregister_dialog(self)

    def accept(self, confirm):
        value = self.password.get()
        if confirm:
            try:
                validate_new_password(value)
            except VaultError as exc:
                messagebox.showerror("密码不符合要求", str(exc), parent=self)
                return
        if confirm and value != self.confirmation.get():
            messagebox.showerror("密码不一致", "两次输入的密码不一致，请重新输入。", parent=self)
            return
        if not value:
            return
        self.result = value
        self.password.set("")
        self.confirmation.set("")
        self.destroy()

    def cancel(self):
        self.password.set("")
        self.confirmation.set("")
        self.destroy()

    def expire(self):
        self.result = None
        self.cancel()


class MergeDialog(tk.Toplevel):
    LABELS = {"new": "新增", "duplicate": "暂判重复", "conflict": "编号冲突",
              "possible_duplicate": "可能重复"}
    def __init__(self, parent, plan):
        super().__init__(parent)
        self.title("确认导入与合并")
        self.transient(parent)
        self.geometry("780x580")
        self.minsize(640, 470)
        self.result = None
        self.plan = plan
        self.choices = {}
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        counts = plan.counts
        ttk.Label(frame, text="  ·  ".join(f"{self.LABELS[k]} {counts.get(k,0)} 项" for k in self.LABELS),
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text="分类基于导入前内容。请逐项处理冲突；确认时按全部选择合并后去重，暂判重复项也可能补入。文件中没有的本地条目继续保留。",
                  wraplength=700).pack(anchor="w", pady=(8, 12))
        table = ttk.Treeview(frame, columns=("kind", "title", "choice"), show="headings", height=8)
        for col, label, width in [("kind", "类型", 95), ("title", "导入标题", 360), ("choice", "处理方式", 165)]:
            table.heading(col, text=label)
            table.column(col, width=width)
        table.pack(fill="both", expand=True)
        self.table = table
        for i, item in enumerate(plan.items):
            if item.kind == "possible_duplicate":
                self.choices[item.incoming.id] = "both"
            table.insert("", "end", iid=str(i), values=(self.LABELS[item.kind], item.incoming.title,
                         "两份都保留" if item.kind == "possible_duplicate" else
                         "请选择" if item.kind == "conflict" else "自动处理"))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=10)
        for label, value in [("保留本地", "local"), ("采用导入内容", "import"), ("两份都保留", "both")]:
            ttk.Button(actions, text=label, command=lambda v=value: self.choose(v)).pack(side="left", padx=(0, 8))
        compare = ttk.Panedwindow(frame, orient="horizontal")
        compare.pack(fill="both", expand=True)
        self.previews = []
        self.preview_labels = []
        for label in ("本地正文（只读）", "导入正文（只读）"):
            part = ttk.Frame(compare)
            heading = ttk.Label(part, text=label, wraplength=340)
            heading.pack(anchor="w")
            self.preview_labels.append(heading)
            text_frame = ttk.Frame(part)
            text_frame.pack(fill="both", expand=True)
            text = tk.Text(text_frame, height=6, wrap="none", font=("Consolas", 10), state="disabled")
            vertical = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
            horizontal = ttk.Scrollbar(text_frame, orient="horizontal", command=text.xview)
            text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            text.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            text_frame.rowconfigure(0, weight=1)
            text_frame.columnconfigure(0, weight=1)
            compare.add(part, weight=1)
            self.previews.append(text)
        table.bind("<<TreeviewSelect>>", self.preview)
        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="确认导入", command=self.accept).pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())
        if plan.items:
            table.selection_set("0")
            self.preview()
        self.grab_set()
        app = getattr(parent, "vault_app", None)
        if app is not None:
            app.register_dialog(self)
        try:
            parent.wait_window(self)
        finally:
            if app is not None:
                app.unregister_dialog(self)

    def expire(self):
        self.result = None
        for widget in self.previews:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.plan = None
        self.choices.clear()
        self.destroy()

    def preview(self, event=None):
        selected = self.table.selection()
        if not selected:
            return
        item = self.plan.items[int(selected[0])]
        self.preview_labels[0].configure(text=f"本地正文（只读） · {item.local.title if item.local else '无本地条目'}")
        self.preview_labels[1].configure(text=f"导入正文（只读） · {item.incoming.title}")
        for widget, value in zip(self.previews, (item.local.body if item.local else "（无本地条目）", item.incoming.body)):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
            widget.configure(state="disabled")

    def choose(self, value):
        selected = self.table.selection()
        if not selected:
            return
        item = self.plan.items[int(selected[0])]
        if item.kind not in ("conflict", "possible_duplicate"):
            return
        if item.kind == "possible_duplicate" and value == "import":
            messagebox.showinfo("可能重复", "不同编号的条目可以保留本地或两份都保留。", parent=self)
            return
        self.choices[item.incoming.id] = value
        self.table.set(selected[0], "choice", {"local": "保留本地", "import": "采用导入内容", "both": "两份都保留"}[value])

    def accept(self):
        if any(item.kind == "conflict" and item.incoming.id not in self.choices for item in self.plan.items):
            messagebox.showwarning("尚未选择", "请为每一项编号冲突选择处理方式。", parent=self)
            return
        self.result = dict(self.choices)
        self.destroy()


class VaultApp:
    def __init__(self, root: tk.Tk, vault: Vault):
        self.root = root
        root.vault_app = self
        self.vault = vault
        self.current_id = None
        self.original = None
        self.draft = None
        self.blocked_draft = None
        self.dialogs = set()
        self.session_epoch = 0
        self.write_depth = 0
        self.checked = set()
        self.loading = False
        self.modal_depth = 0
        self.last_activity = time.monotonic()
        root.title(f"本地密匣  {__version__}")
        root.geometry("1000x680")
        root.minsize(800, 520)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=30)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        top = ttk.Frame(root, padding=(18, 14))
        top.pack(fill="x")
        ttk.Label(top, text="本地密匣", font=("Microsoft YaHei UI", 19, "bold")).pack(side="left")
        self.lock_label = ttk.Label(top, text="", foreground="#666666")
        self.lock_label.pack(side="left", padx=18)
        ttk.Button(top, text="立即锁定", command=self.manual_lock).pack(side="right")
        ttk.Button(top, text="设置", command=self.settings).pack(side="right", padx=8)
        menu_button = ttk.Menubutton(top, text="保险库")
        menu = tk.Menu(menu_button, tearoff=False)
        menu.add_command(label="导出全部条目…", command=lambda: self.export(False))
        menu.add_command(label="导出勾选条目…", command=lambda: self.export(True))
        menu.add_command(label="导入并合并…", command=self.import_entries)
        menu.add_separator()
        menu.add_command(label="完整加密备份…", command=self.backup)
        menu.add_command(label="从完整备份恢复…", command=self.restore)
        menu.add_command(label="修改主密码…", command=self.change_password)
        menu_button["menu"] = menu
        menu_button.pack(side="right")
        panes = ttk.Panedwindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=18)
        left = ttk.Frame(panes, width=280)
        right = ttk.Frame(panes, padding=(18, 0, 0, 0))
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        ttk.Label(left, text="按标题搜索").pack(anchor="w")
        self.search = tk.StringVar()
        ttk.Entry(left, textvariable=self.search).pack(fill="x", pady=(5, 10))
        self.search.trace_add("write", lambda *a: self.refresh_list())
        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(list_frame, columns=("check", "title"), show="headings", selectmode="browse")
        self.tree.heading("check", text="勾选")
        self.tree.column("check", width=48, minwidth=48, stretch=False, anchor="center")
        self.tree.heading("title", text="标题")
        self.tree.column("title", width=215, minwidth=120)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.tree_click)
        self.tree.bind("<<TreeviewSelect>>", self.select_entry)
        self.tree.bind("<space>", self.toggle_selected)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=10)
        ttk.Button(row, text="新增条目", command=self.new_entry).pack(side="left")
        ttk.Button(row, text="删除条目", command=self.delete_entry).pack(side="right")
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Button(row, text="勾选当前结果", command=self.check_visible).pack(side="left")
        ttk.Button(row, text="清空勾选", command=self.clear_checked).pack(side="right")
        self.check_label = ttk.Label(left, text="已勾选 0 项")
        self.check_label.pack(anchor="w", pady=(8, 0))
        ttk.Label(right, text="标题").pack(anchor="w")
        self.title = tk.StringVar()
        self.title_entry = ttk.Entry(right, textvariable=self.title)
        self.title_entry.pack(fill="x", pady=(5, 12))
        ttk.Label(right, text="正文 · 纯文本，可自由换行和手动复制").pack(anchor="w")
        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True, pady=(5, 10))
        self.body = tk.Text(text_frame, wrap="none", undo=True, font=("Consolas", 11),
                            padx=10, pady=10, borderwidth=1, relief="solid")
        vertical = ttk.Scrollbar(text_frame, orient="vertical", command=self.body.yview)
        horizontal = ttk.Scrollbar(text_frame, orient="horizontal", command=self.body.xview)
        self.body.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.body.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        context = tk.Menu(self.body, tearoff=False)
        for label, event in [("复制", "<<Copy>>"), ("粘贴", "<<Paste>>"), ("剪切", "<<Cut>>")]:
            context.add_command(label=label, command=lambda e=event: self.body.event_generate(e))
        context.add_separator()
        context.add_command(label="全选", command=self.select_all)
        self.body.bind("<Button-3>", lambda e: context.tk_popup(e.x_root, e.y_root))
        self.body.bind("<Control-a>", self.select_all)
        self.body.bind("<Control-A>", self.select_all)
        bottom = ttk.Frame(right)
        bottom.pack(fill="x")
        self.save_button = ttk.Button(bottom, text="保存", command=self.save)
        self.save_button.pack(side="right")
        ttk.Label(bottom, text="编辑后请点击保存；切换和关闭时会提示处理修改。").pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(root, textvariable=self.status, padding=(18, 12), wraplength=950).pack(fill="x")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind_all("<KeyPress>", self.activity, add="+")
        root.bind_all("<ButtonPress>", self.activity, add="+")
        root.bind_all("<MouseWheel>", self.activity, add="+")
        root.bind("<Control-s>", lambda e: self.save())
        self.set_editor(None)
        self.refresh_list()
        self.update_state()
        root.after(1000, self.idle_tick)
        if not vault.is_initialized:
            root.after(150, self.first_setup)

    def activity(self, event=None):
        self.last_activity = time.monotonic()

    def register_dialog(self, window):
        self.dialogs.add(window)

    def unregister_dialog(self, window):
        self.dialogs.discard(window)

    def expire_dialogs(self):
        for window in tuple(self.dialogs):
            if window.winfo_exists():
                if hasattr(window, "expire"):
                    window.expire()
                else:
                    window.destroy()
        self.dialogs.clear()

    @contextmanager
    def writing(self):
        """已经开始的写入先提交或失败回滚，再处理闲置锁定。"""
        self.write_depth += 1
        try:
            yield
        finally:
            self.write_depth -= 1
            self.check_idle()

    def check_idle(self):
        minutes = self.vault.idle_minutes
        if (self.vault.is_unlocked and minutes and not self.write_depth
                and time.monotonic() - self.last_activity >= minutes * 60):
            self.lock_for_idle()

    def lock_for_idle(self):
        failure = None
        if self.dirty():
            entry = self.editor_entry()
            try:
                self.draft = self.vault.seal_draft(entry)
            except Exception as exc:
                # 异常草稿仅保留在内存，先遮蔽再提示，绝不冒称已加密。
                self.blocked_draft = entry
                failure = exc
        self.vault.lock()
        self.session_epoch += 1
        self.set_editor(None)
        self.expire_dialogs()
        self.update_state()
        if failure is not None:
            self.status.set("正文已遮蔽并停止访问；未保存编辑加密失败，仅暂存在内存。解锁后可继续处理，意外退出会丢失。")
            messagebox.showerror("未保存编辑尚未成功加密",
                f"{failure}\n\n正文已遮蔽，保险库访问已停止。未保存编辑仍在内存，尚未写入文件。"
                "\n请重新解锁后保存或放弃；关闭时仍可选择保存、放弃或取消。意外退出会丢失这些编辑。",
                parent=self.root)
        else:
            self.status.set("已自动锁定。未保存编辑已加密暂存，解锁后恢复；关闭时可保存、放弃或取消退出。" if self.draft else "闲置时间已到，保险库已自动锁定。")

    def continue_session(self, epoch, *, require_unlocked=True):
        self.check_idle()
        if epoch != self.session_epoch or (require_unlocked and not self.vault.is_unlocked):
            self.status.set("闲置锁定已取消待处理操作。请重新解锁后重新开始。")
            return False
        return True

    def file_dialog(self, chooser, **options):
        """选择文件期间提前遮蔽正文；返回时先检查锁定。"""
        epoch = self.session_epoch
        entry = self.editor_entry() if str(self.body.cget("state")) != "disabled" else None
        original = self.original
        self.set_editor(None)
        try:
            return chooser(parent=self.root, **options)
        finally:
            self.check_idle()
            if epoch == self.session_epoch and self.vault.is_unlocked and entry is not None:
                self.set_editor(entry)
                self.original = original

    def dialog_password(self, title, prompt, confirm=False):
        self.modal_depth += 1
        try:
            return PasswordDialog(self.root, title, prompt, confirm).result
        finally:
            self.modal_depth -= 1
            self.check_idle()

    def error(self, exc):
        messagebox.showerror("操作未完成", str(exc), parent=self.root)

    @interaction
    def first_setup(self):
        password = self.dialog_password("首次设置", "为本地保险库设置主密码。标题可直接查看，正文解锁后才能读取。", True)
        if password is None:
            self.status.set("尚未设置主密码。点击新增条目可重新设置。")
            return False
        try:
            self.vault.create(password)
            self.update_state()
            self.status.set("主密码已设置。点击新增条目，开始保存文本。")
            return True
        except (VaultError, OSError) as exc:
            self.error(exc)
            return False

    @interaction
    def ensure_unlocked(self):
        if not self.vault.is_initialized:
            return self.first_setup()
        if self.vault.is_unlocked:
            return True
        password = self.dialog_password("解锁保险库", "请输入主密码。本次解锁后可以连续查看和编辑条目。")
        if password is None:
            return False
        try:
            self.vault.unlock(password)
            self.activity()  # 成功输入主密码开启新的访问时段。
            if self.draft is not None:
                entry = self.vault.open_draft(self.draft)
                self.draft = None
                self.set_editor(entry)
                self.original = None  # 恢复的草稿仍必须明确保存。
                self.status.set("已恢复自动锁定前的未保存编辑，请点击保存。")
            elif self.blocked_draft is not None:
                entry = self.blocked_draft
                self.blocked_draft = None
                self.set_editor(entry)
                self.original = None
                self.status.set("已恢复加密失败时遮蔽的未保存编辑，请保存或明确放弃。")
            self.update_state()
            return True
        except (VaultError, OSError) as exc:
            self.vault.lock()
            self.set_editor(None)
            self.error(exc)
            return False

    def update_state(self):
        self.lock_label.configure(text="已解锁" if self.vault.is_unlocked else
            "已停止访问 · 未保存编辑尚未加密" if self.blocked_draft is not None else "已锁定 · 标题仍可查看")
        if not self.vault.is_unlocked and self.draft is None and self.blocked_draft is None:
            self.status.set("点击条目并输入主密码，即可查看正文。")

    def refresh_list(self):
        self.loading = True
        try:
            self.tree.delete(*self.tree.get_children())
            search = self.search.get().casefold()
            titles = self.vault.list_titles()
            valid = {entry_id for entry_id, title in titles}
            self.checked.intersection_update(valid)
            for entry_id, title in titles:
                if search in title.casefold():
                    self.tree.insert("", "end", iid=entry_id,
                                     values=("☑" if entry_id in self.checked else "☐", title))
            if self.current_id and self.tree.exists(self.current_id):
                self.tree.selection_set(self.current_id)
            self.check_label.configure(text=f"已勾选 {len(self.checked)} 项（含搜索结果外的勾选）")
        finally:
            self.loading = False

    def tree_click(self, event):
        if self.tree.identify_column(event.x) == "#1":
            entry_id = self.tree.identify_row(event.y)
            if entry_id:
                self.toggle_check(entry_id)
            return "break"

    def toggle_selected(self, event=None):
        selected = self.tree.selection()
        if selected:
            self.toggle_check(selected[0])
        return "break"

    def toggle_check(self, entry_id):
        if entry_id in self.checked:
            self.checked.remove(entry_id)
        else:
            self.checked.add(entry_id)
        self.tree.set(entry_id, "check", "☑" if entry_id in self.checked else "☐")
        self.check_label.configure(text=f"已勾选 {len(self.checked)} 项（含搜索结果外的勾选）")

    def check_visible(self):
        self.checked.update(self.tree.get_children())
        self.refresh_list()

    def clear_checked(self):
        self.checked.clear()
        self.refresh_list()

    def editor_entry(self):
        return Entry(self.current_id or "", self.title.get(), self.body.get("1.0", "end-1c"))

    def dirty(self):
        if self.draft is not None or self.blocked_draft is not None:
            return True
        if str(self.body.cget("state")) == "disabled":
            return False
        entry = self.editor_entry()
        return self.original is None or (entry.title, entry.body) != self.original

    def set_editor(self, entry):
        self.loading = True
        try:
            self.body.configure(state="normal")
            self.body.delete("1.0", "end")
            self.title.set(entry.title if entry else "")
            if entry:
                self.body.insert("1.0", entry.body)
                self.current_id = entry.id or None
                self.original = (entry.title, entry.body)
            else:
                self.current_id = None
                self.original = None
            self.body.edit_reset()
            state = "normal" if entry is not None else "disabled"
            self.title_entry.configure(state=state)
            self.body.configure(state=state)
            self.save_button.configure(state=state)
        finally:
            self.loading = False

    @interaction
    def resolve_dirty(self, *, closing=False):
        if not self.dirty():
            return True
        if closing and (self.draft is not None or self.blocked_draft is not None) and not self.vault.is_unlocked:
            protected = "未保存编辑加密失败，正文已遮蔽，编辑仅暂存在内存。" if self.blocked_draft is not None else "自动锁定前的未保存编辑仍加密暂存在内存。"
            choice = messagebox.askyesnocancel(
                "锁定草稿尚未保存",
                protected + "\n是否保存并退出？\n\n"
                "“是”：解锁、保存并退出；\n“否”：无需解锁，放弃草稿并退出；\n"
                "“取消”：取消退出，保留草稿并继续锁定。",
                parent=self.root)
            if choice is None:
                return False
            if not choice:
                self.draft = None
                self.blocked_draft = None
                self.set_editor(None)
                return True
            sealed = self.draft
            blocked = self.blocked_draft
            if self.ensure_unlocked() and self.save():
                return True
            # 解锁会把草稿移到编辑器；退出失败时恢复原加密草稿和锁定状态。
            self.draft = sealed
            self.blocked_draft = blocked
            self.vault.lock()
            self.set_editor(None)
            self.update_state()
            self.status.set("退出已取消。未保存编辑仍被遮蔽且尚未成功加密，可解锁处理或再次选择放弃退出。" if blocked is not None else "退出已取消。未保存编辑仍加密暂存，可解锁恢复或再次选择放弃退出。")
            return False
        if (self.draft is not None or self.blocked_draft is not None) and not self.ensure_unlocked():
            return False
        epoch = self.session_epoch
        choice = messagebox.askyesnocancel("尚未保存", "当前条目有未保存的修改。\n是否保存？\n\n“是”：保存；“否”：放弃；“取消”：继续编辑。", parent=self.root)
        if not self.continue_session(epoch):
            return False
        if choice is None:
            return False
        if choice:
            return self.save()
        self.draft = None
        self.blocked_draft = None
        if self.current_id:
            try:
                self.set_editor(self.vault.get_entry(self.current_id))
            except (VaultError, OSError) as exc:
                self.error(exc)
                return False
        else:
            self.set_editor(None)
        return True

    @interaction
    def select_entry(self, event=None):
        if self.loading:
            return
        selected = self.tree.selection()
        if not selected:
            return
        entry_id = selected[0]
        if entry_id == self.current_id and self.vault.is_unlocked and self.draft is None:
            return
        previous = self.current_id
        if not self.resolve_dirty() or not self.ensure_unlocked():
            self.loading = True
            if previous and self.tree.exists(previous):
                self.tree.selection_set(previous)
            else:
                self.tree.selection_remove(*self.tree.selection())
            self.root.after_idle(lambda: setattr(self, "loading", False))
            return
        try:
            self.set_editor(self.vault.get_entry(entry_id))
            if self.tree.exists(entry_id):
                self.tree.selection_set(entry_id)
            self.status.set("已打开条目。编辑完成后点击保存。")
        except (VaultError, OSError) as exc:
            self.error(exc)

    def select_all(self, event=None):
        self.body.tag_add("sel", "1.0", "end-1c")
        self.body.mark_set("insert", "1.0")
        return "break"

    @interaction
    def new_entry(self):
        if not self.resolve_dirty() or not self.ensure_unlocked():
            return
        self.set_editor(Entry("", "", ""))
        self.original = None
        self.tree.selection_remove(*self.tree.selection())
        self.title_entry.focus_set()
        self.status.set("填写标题与正文，然后点击保存。")

    @interaction
    def save(self):
        if not self.ensure_unlocked() or str(self.body.cget("state")) == "disabled":
            return False
        entry = self.editor_entry()
        if not entry.title.strip():
            messagebox.showwarning("请输入标题", "标题不能为空。正文可以留空。", parent=self.root)
            return False
        try:
            with self.writing():
                entry_id = self.vault.put_entry(entry.title, entry.body, entry.id or None)
                self.current_id = entry_id
                self.original = (entry.title, entry.body)
                self.refresh_list()
                self.status.set("已保存，正文已自动加密。")
            return True
        except (VaultError, OSError) as exc:
            self.error(exc)
            return False

    @interaction
    def delete_entry(self):
        selected = self.tree.selection()
        entry_id = selected[0] if selected else self.current_id
        if not entry_id:
            return
        title = dict(self.vault.list_titles()).get(entry_id, "")
        epoch = self.session_epoch
        if not messagebox.askyesno("删除条目", f"确认删除“{title}”？\n当前条目的未保存编辑也将丢弃。此操作会立即保存。", parent=self.root):
            return
        if not self.continue_session(epoch, require_unlocked=False):
            return
        try:
            if not self.ensure_unlocked():
                return
            with self.writing():
                self.vault.delete(entry_id)
                if self.current_id == entry_id:
                    self.draft = None
                    self.blocked_draft = None
                    self.set_editor(None)
                self.checked.discard(entry_id)
                self.refresh_list()
                self.status.set("条目已删除并保存。")
        except (VaultError, OSError) as exc:
            self.error(exc)

    @interaction
    def manual_lock(self):
        if not self.vault.is_unlocked:
            return
        if not self.resolve_dirty():
            return
        self.vault.lock()
        self.session_epoch += 1
        self.set_editor(None)
        self.expire_dialogs()
        self.update_state()
        self.status.set("已锁定。再次查看正文时需要主密码。")

    def idle_tick(self):
        try:
            self.check_idle()
        except (VaultError, OSError) as exc:
            self.error(exc)
        finally:
            self.root.after(1000, self.idle_tick)

    @interaction
    def export(self, selected_only):
        ids = sorted(self.checked) if selected_only else None
        if selected_only and not ids:
            messagebox.showinfo("未勾选条目", "请先勾选需要导出的条目。", parent=self.root)
            return
        if not self.resolve_dirty() or not self.ensure_unlocked():
            return
        epoch = self.session_epoch
        filename = self.file_dialog(filedialog.asksaveasfilename, title="保存加密导出文件", defaultextension=".lvexport", filetypes=[("加密导出文件", "*.lvexport")])
        if not filename or not self.continue_session(epoch):
            return
        password = self.dialog_password("设置导出密码", "为此导出文件设置密码。在另一台电脑导入时使用这个密码。", True)
        if password is None or not self.continue_session(epoch):
            return
        try:
            with self.writing():
                self.vault.export_file(filename, password, ids)
                self.status.set(f"已加密导出 {len(ids) if ids is not None else len(self.vault.list_titles())} 项。本地条目未改变。")
        except (VaultError, OSError) as exc:
            self.error(exc)

    @interaction
    def import_entries(self):
        if not self.resolve_dirty() or not self.ensure_unlocked():
            return
        epoch = self.session_epoch
        filename = self.file_dialog(filedialog.askopenfilename, title="选择加密导出文件", filetypes=[("加密导出文件", "*.lvexport"), ("所有文件", "*.*")])
        if not filename or not self.continue_session(epoch):
            return
        password = self.dialog_password("输入导出密码", "请输入这个导出文件的密码。")
        if password is None or not self.continue_session(epoch):
            return
        self.modal_depth += 1
        try:
            plan = self.vault.inspect_import(filename, password)
            if not self.continue_session(epoch):
                return
            choices = MergeDialog(self.root, plan).result
            if not self.continue_session(epoch):
                return
            if choices is None:
                self.status.set("已取消导入，本地数据未改变。")
                return
            with self.writing():
                self.vault.commit_import(plan, choices)
                self.set_editor(None)
                self.refresh_list()
                self.status.set("导入已完成，使用当前保险库的主密码加密保存。")
        except (VaultError, OSError) as exc:
            self.error(exc)
        finally:
            self.modal_depth -= 1
            self.check_idle()

    @interaction
    def backup(self):
        if not self.vault.is_initialized or not self.resolve_dirty():
            return
        epoch = self.session_epoch
        filename = self.file_dialog(filedialog.asksaveasfilename, title="完整加密备份", defaultextension=".lvbackup", filetypes=[("完整加密备份", "*.lvbackup")])
        if not filename or not self.continue_session(epoch, require_unlocked=False):
            return
        try:
            with self.writing():
                self.vault.backup_file(filename)
                self.status.set("完整加密备份已保存。恢复时需要备份当时的主密码。")
        except (VaultError, OSError) as exc:
            self.error(exc)

    @interaction
    def restore(self):
        if not self.resolve_dirty():
            return
        epoch = self.session_epoch
        filename = self.file_dialog(filedialog.askopenfilename, title="选择完整加密备份", filetypes=[("完整加密备份", "*.lvbackup"), ("所有文件", "*.*")])
        if not filename or not self.continue_session(epoch, require_unlocked=False):
            return
        password = self.dialog_password("恢复备份", "请输入备份创建时的主密码。恢复会替换全部本地条目。")
        if password is None or not self.continue_session(epoch, require_unlocked=False):
            return
        if not messagebox.askyesno("确认恢复", "恢复会替换当前保险库的全部内容。\n恢复来源会保留，不额外生成旧版本副本。\n\n确认继续？", parent=self.root):
            return
        if not self.continue_session(epoch, require_unlocked=False):
            return
        try:
            with self.writing():
                self.vault.restore_backup(filename, password)
                self.draft = None
                self.blocked_draft = None
                self.set_editor(None)
                self.checked.clear()
                self.refresh_list()
                self.update_state()
                self.status.set("已恢复完整备份。当前主密码已变为备份当时的主密码。")
        except (VaultError, OSError) as exc:
            self.error(exc)

    @interaction
    def change_password(self):
        if not self.resolve_dirty() or not self.ensure_unlocked():
            return
        epoch = self.session_epoch
        password = self.dialog_password("修改主密码", "设置新的主密码。现有正文会继续正常读取。", True)
        if password is None or not self.continue_session(epoch):
            return
        try:
            with self.writing():
                self.vault.change_password(password)
                self.status.set("主密码已修改，当前库的密码派生已加强。已有备份和导出仍使用各自原密码。")
        except (VaultError, OSError) as exc:
            self.error(exc)

    @interaction
    def settings(self):
        if not self.ensure_unlocked():
            return
        epoch = self.session_epoch
        self.modal_depth += 1
        window = tk.Toplevel(self.root)
        window.title("设置")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="闲置自动锁定（分钟，0 表示关闭）").pack(anchor="w")
        minutes = tk.StringVar(value=str(self.vault.idle_minutes))
        ttk.Spinbox(frame, from_=0, to=120, textvariable=minutes, width=12).pack(anchor="w", pady=8)
        ttk.Label(frame, text="范围：0～120 分钟。自动锁定前的未保存编辑仅加密暂存于内存，\n解锁后恢复；程序退出或意外终止后暂存消失。").pack(anchor="w", pady=(0, 12))
        def apply():
            if not self.continue_session(epoch):
                return
            try:
                value = int(minutes.get())
                if not 0 <= value <= 120:
                    raise ValueError()
                with self.writing():
                    self.vault.set_idle_minutes(value)
                    self.status.set("自动锁定设置已保存。")
                    window.destroy()
            except ValueError:
                messagebox.showerror("数值无效", "请输入 0～120 的整数。", parent=window)
            except (VaultError, OSError) as exc:
                self.error(exc)
        ttk.Button(frame, text="保存设置", command=apply).pack(anchor="e")
        window.grab_set()
        self.register_dialog(window)
        try:
            self.root.wait_window(window)
        finally:
            self.modal_depth -= 1
            self.unregister_dialog(window)
            self.check_idle()

    @interaction
    def close(self):
        if not self.resolve_dirty(closing=True):
            return
        self.vault.lock()
        self.set_editor(None)
        self.root.destroy()


class InstanceLock:
    """每个数据目录只允许一个窗口，避免两个实例互相覆盖。"""
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "application.lock").open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise VaultError("此保险库已在另一个窗口打开，请使用已有窗口。") from exc

    def close(self):
        self.file.close()


class InstallerMutex:
    """让安装程序识别运行中的应用，避免升级期间替换文件。"""
    def __init__(self):
        self.handle = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.handle = self.kernel.CreateMutexW(None, False, "LocalVaultAppMutex")
            if not self.handle:
                raise VaultError("无法建立安装保护，请重新打开应用。")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def smoke_test():
    """在临时目录验证打包后的加密、合并与实际文本组件，无真实数据。"""
    with tempfile.TemporaryDirectory(prefix="local-vault-smoke-") as directory:
        folder = Path(directory)
        vault = Vault(folder / "vault.lvault")
        vault.create("虚构自检主密码-12345-长口令")
        original = "中文示例\n  空格保留  \n密钥：FAKE-ONLY\n" + "长文本甲乙丙\n" * 500
        entry_id = vault.put_entry("虚构标题", original)
        vault.lock()
        vault = Vault(folder / "vault.lvault")
        vault.unlock("虚构自检主密码-12345-长口令")
        assert vault.get_entry(entry_id).body == original
        vault.export_file(folder / "transfer.lvexport", "虚构自检导出密码-12345-长口令")
        other = Vault(folder / "other.lvault")
        other.create("另一虚构自检密码-12345-长口令")
        plan = other.inspect_import(folder / "transfer.lvexport", "虚构自检导出密码-12345-长口令")
        other.commit_import(plan, {})
        assert other.get_entry(entry_id).body == original
        root = tk.Tk()
        root.withdraw()
        app = VaultApp(root, other)
        app.set_editor(other.get_entry(entry_id))
        root.update()
        assert app.body.get("1.0", "end-1c") == original
        app.close()
    return {"应用": "本地密匣", "版本": __version__, "结果": "通过", "验证": "保存重开、加密导出导入、中文长文本和桌面文本框"}


def main():
    parser = argparse.ArgumentParser(description="本地密匣：离线加密文本管理")
    parser.add_argument("--data-dir", type=Path, help="指定数据目录（开发与虚构测试隔离）")
    parser.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument("--smoke-test", action="store_true", help="执行虚构数据自测")
    parser.add_argument("--report", type=Path, help="将版本或自测结果写入文件")
    args = parser.parse_args()
    if args.version or args.smoke_test:
        result = smoke_test() if args.smoke_test else {"版本": __version__}
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(text, encoding="utf-8")
        if sys.stdout:
            print(text)
        return
    root = tk.Tk()
    root.withdraw()
    directory = args.data_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LocalVault"
    instance = None
    installer_mutex = None
    try:
        instance = InstanceLock(directory)
        installer_mutex = InstallerMutex()
        vault_path = directory / "vault.lvault"
        try:
            vault = Vault(vault_path)
        except VaultError as exc:
            if not messagebox.askyesno("保险库无法读取", f"{exc}\n\n原文件会保留。是否选择完整加密备份进行恢复？", parent=root):
                return
            filename = filedialog.askopenfilename(parent=root, title="恢复完整加密备份", filetypes=[("完整加密备份", "*.lvbackup"), ("所有文件", "*.*")])
            if not filename:
                return
            password = PasswordDialog(root, "恢复备份", "请输入备份创建时的主密码。恢复会替换全部本地内容，恢复来源会保留。").result
            if password is None:
                return
            vault = Vault.for_restore(vault_path)
            vault.restore_backup(filename, password)
        VaultApp(root, vault)
        root.deiconify()
        root.mainloop()
    except (VaultError, OSError) as exc:
        messagebox.showerror("本地密匣无法打开", f"{exc}\n\n数据目录：{directory}\n原数据不会被重新初始化。", parent=root)
    finally:
        if instance is not None:
            instance.close()
        if installer_mutex is not None:
            installer_mutex.close()


if __name__ == "__main__":
    main()
