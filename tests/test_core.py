"""使用完全虚构的数据验证保险库保存、密码保护和跨电脑合并。"""

import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import local_vault.core as core
from local_vault.core import Vault, VaultError


MASTER = "虚构主密码-2026-Only-Example!"
OTHER_MASTER = "另一台虚构主密码-2026-Example!"
EXPORT_PASSWORD = "虚构导出密码-2026-Example!"
WRONG = "这是错误的虚构密码!"


@pytest.fixture
def vault(tmp_path):
    instance = Vault(tmp_path / "vault.json")
    instance.create(MASTER)
    return instance


@pytest.fixture
def samples():
    path = Path(__file__).parent / "fixtures" / "fake_entries.json"
    return json.loads(path.read_text(encoding="utf-8"))


def add_samples(vault, samples):
    return [vault.put_entry(item["title"], item["body"]) for item in samples]


def contents(vault):
    return {
        entry_id: (vault.get_entry(entry_id).title, vault.get_entry(entry_id).body)
        for entry_id, _ in vault.list_titles()
    }


def initialized_pair(tmp_path):
    source = Vault(tmp_path / "source.json")
    source.create(MASTER)
    target = Vault(tmp_path / "target.json")
    target.create(OTHER_MASTER)
    return source, target


def export(source, tmp_path, ids=None):
    output = tmp_path / "transfer.lvexport"
    source.export_file(output, EXPORT_PASSWORD, ids=ids)
    return output


def mutate_json(path, selector, replacement):
    data = json.loads(path.read_text(encoding="utf-8"))

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and selector(key, item):
                    value[key] = replacement(item)
                    return True
                if visit(item):
                    return True
        elif isinstance(value, list):
            for item in value:
                if visit(item):
                    return True
        return False

    assert visit(data), "测试未找到需篡改的字段，请检查文件格式"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def corrupt_ciphertext(path):
    mutate_json(
        path,
        lambda key, value: ("cipher" in key or key in {"body", "ct"}) and len(value) > 4,
        lambda value: ("A" if value[0] != "A" else "B") + value[1:],
    )


def test_initialization_locked_titles_and_restart(tmp_path, samples):
    path = tmp_path / "vault.json"
    instance = Vault(path)
    assert not instance.is_initialized
    instance.create(MASTER)
    assert instance.is_initialized and instance.is_unlocked
    ids = add_samples(instance, samples)
    instance.lock()
    assert not instance.is_unlocked
    assert dict(instance.list_titles()) == dict(zip(ids, [x["title"] for x in samples]))
    with pytest.raises(VaultError):
        instance.get_entry(ids[0])
    reopened = Vault(path)
    assert reopened.is_initialized and not reopened.is_unlocked
    reopened.unlock(MASTER)
    for entry_id, sample in zip(ids, samples):
        entry = reopened.get_entry(entry_id)
        assert (entry.id, entry.title, entry.body) == (entry_id, sample["title"], sample["body"])


def test_plain_titles_and_encrypted_bodies(vault):
    title = "公开的虚构条目标题"
    body = "绝不能明文落盘的虚构正文-FakeSecret-9f817"
    entry_id = vault.put_entry(title, body)
    raw = vault.path.read_text(encoding="utf-8")
    assert title in raw
    assert body not in raw
    assert entry_id in raw
    assert MASTER not in raw


def test_exact_long_chinese_whitespace_and_line_endings(vault):
    body = "\r\n  中文 AbC\t空格  \r\n\n" + "  长正文测试 AbCdEf  \r\n" * 10000 + "尾部\n\r\n   "
    entry_id = vault.put_entry("中文长文本", body)
    reopened = Vault(vault.path)
    reopened.unlock(MASTER)
    assert reopened.get_entry(entry_id).body == body


def test_empty_and_whitespace_body_preserved(vault):
    for body in ["", "   ", "\n\r\n\t"]:
        entry_id = vault.put_entry("虚构空正文", body)
        assert vault.get_entry(entry_id).body == body


def test_wrong_master_password_preserves_file(vault):
    entry_id = vault.put_entry("虚构账号", "仅供测试")
    original = vault.path.read_bytes()
    vault.lock()
    with pytest.raises(VaultError):
        vault.unlock(WRONG)
    assert not vault.is_unlocked
    assert vault.path.read_bytes() == original
    vault.unlock(MASTER)
    assert vault.get_entry(entry_id).body == "仅供测试"


@pytest.mark.parametrize("tampering", ["ciphertext", "title", "malformed"])
def test_local_tampering_rejected(tmp_path, tampering):
    path = tmp_path / "vault.json"
    instance = Vault(path)
    instance.create(MASTER)
    instance.put_entry("虚构标题", "虚构正文")
    if tampering == "ciphertext":
        corrupt_ciphertext(path)
    elif tampering == "title":
        mutate_json(path, lambda key, value: key == "title", lambda value: value + "被篡改")
    else:
        path.write_text("{损坏文件", encoding="utf-8")
    damaged = path.read_bytes()
    with pytest.raises(VaultError):
        reopened = Vault(path)
        reopened.unlock(MASTER)
        for entry_id, _ in reopened.list_titles():
            reopened.get_entry(entry_id)
    assert path.read_bytes() == damaged


def test_update_title_body_and_delete(vault):
    first = vault.put_entry("原虚构标题", "原正文")
    second = vault.put_entry("保留条目", "保留正文")
    assert vault.put_entry("新虚构标题", "修改后的\n正文", entry_id=first) == first
    assert contents(vault)[first] == ("新虚构标题", "修改后的\n正文")
    vault.delete(first)
    reopened = Vault(vault.path)
    reopened.unlock(MASTER)
    assert contents(reopened) == {second: ("保留条目", "保留正文")}


@pytest.mark.parametrize("operation", ["edit", "delete", "password"])
def test_atomic_replace_failure_preserves_disk_and_memory(vault, monkeypatch, operation):
    entry_id = vault.put_entry("原始标题", "原始正文")
    original_bytes = vault.path.read_bytes()
    before = contents(vault)

    def fail_replace(*args, **kwargs):
        raise PermissionError("模拟文件被占用")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(VaultError):
            if operation == "edit":
                vault.put_entry("未保存的新标题", "未保存的新正文", entry_id=entry_id)
            elif operation == "delete":
                vault.delete(entry_id)
            else:
                vault.change_password(OTHER_MASTER)
    assert vault.path.read_bytes() == original_bytes
    assert contents(vault) == before
    reopened = Vault(vault.path)
    reopened.unlock(MASTER)
    assert contents(reopened) == before


def test_import_save_failure_rolls_back_all_entries(tmp_path, monkeypatch, samples):
    source, target = initialized_pair(tmp_path)
    add_samples(source, samples)
    original_id = target.put_entry("原有虚构条目", "不得丢失")
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    original_bytes = target.path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise PermissionError("模拟导入保存失败")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(VaultError):
            target.commit_import(plan, {})
    assert target.path.read_bytes() == original_bytes
    assert contents(target) == {original_id: ("原有虚构条目", "不得丢失")}


@pytest.mark.parametrize("invalidate", ["reused", "lock"])
def test_import_plan_cannot_be_reused_or_survive_lock(tmp_path, invalidate):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构新条目", "虚构内容")
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    if invalidate == "reused":
        target.commit_import(plan, {})
    else:
        target.lock()
        target.unlock(OTHER_MASTER)
    original = target.path.read_bytes()
    with pytest.raises(VaultError):
        target.commit_import(plan, {})
    assert target.path.read_bytes() == original


@pytest.mark.parametrize("partial", [False, True])
def test_full_and_partial_export_cross_password(tmp_path, samples, partial):
    source, target = initialized_pair(tmp_path)
    ids = add_samples(source, samples)
    selected = ids[:2] if partial else ids
    source_before = contents(source)
    file = export(source, tmp_path, ids=selected if partial else None)
    raw = file.read_text(encoding="utf-8")
    for entry_id in selected:
        assert source.get_entry(entry_id).title in raw
        assert source.get_entry(entry_id).body not in raw
    plan = target.inspect_import(file, EXPORT_PASSWORD)
    assert plan.counts["new"] == len(selected)
    target.commit_import(plan, {})
    assert contents(target) == {entry_id: source_before[entry_id] for entry_id in selected}
    assert contents(source) == source_before
    target.lock()
    with pytest.raises(VaultError):
        target.unlock(MASTER)
    target.unlock(OTHER_MASTER)
    assert contents(target) == {entry_id: source_before[entry_id] for entry_id in selected}


@pytest.mark.parametrize("tampering", ["password", "ciphertext", "title", "malformed"])
def test_bad_export_never_changes_target(tmp_path, tampering):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构导入条目", "虚构导入正文")
    target.put_entry("原有条目", "必须保留的正文")
    file = export(source, tmp_path)
    if tampering == "ciphertext":
        corrupt_ciphertext(file)
    elif tampering == "title":
        mutate_json(file, lambda key, value: key == "title", lambda value: value + "被篡改")
    elif tampering == "malformed":
        file.write_text("非有效导出文件", encoding="utf-8")
    original = target.path.read_bytes()
    before = contents(target)
    with pytest.raises(VaultError):
        target.inspect_import(file, WRONG if tampering == "password" else EXPORT_PASSWORD)
    assert target.path.read_bytes() == original
    assert contents(target) == before


def test_inspection_and_cancel_are_read_only(tmp_path):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构新增", "虚构内容")
    original = target.path.read_bytes()
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    assert plan.counts["new"] == 1
    assert target.path.read_bytes() == original
    assert target.list_titles() == []


def test_global_content_deduplication_with_different_ids(tmp_path):
    source, target = initialized_pair(tmp_path)
    incoming_id = source.put_entry("同一虚构标题", "精确相同正文\n  AbC  ")
    local_id = target.put_entry("同一虚构标题", "精确相同正文\n  AbC  ")
    assert incoming_id != local_id
    file = export(source, tmp_path)
    for _ in range(2):
        plan = target.inspect_import(file, EXPORT_PASSWORD)
        assert plan.counts["duplicate"] == 1
        target.commit_import(plan, {})
        assert contents(target) == {local_id: ("同一虚构标题", "精确相同正文\n  AbC  ")}


@pytest.mark.parametrize("choice", ["local", "import", "both"])
def test_same_id_conflict_choices_and_repeat_import(tmp_path, choice):
    source, target = initialized_pair(tmp_path)
    entry_id = source.put_entry("最初的虚构条目", "最初的正文")
    file = export(source, tmp_path)
    target.commit_import(target.inspect_import(file, EXPORT_PASSWORD), {})
    target.put_entry("本地修改标题", "本地修改正文", entry_id=entry_id)
    source.put_entry("导入修改标题", "导入修改正文", entry_id=entry_id)
    source.export_file(file, EXPORT_PASSWORD)
    plan = target.inspect_import(file, EXPORT_PASSWORD)
    assert plan.counts["conflict"] == 1
    item = next(item for item in plan.items if item.kind == "conflict")
    assert item.incoming.id == entry_id
    assert item.local.id == entry_id
    target.commit_import(plan, {entry_id: choice})
    if choice == "local":
        assert contents(target) == {entry_id: ("本地修改标题", "本地修改正文")}
    elif choice == "import":
        assert contents(target) == {entry_id: ("导入修改标题", "导入修改正文")}
    else:
        result = contents(target)
        assert result[entry_id] == ("本地修改标题", "本地修改正文")
        copies = [other_id for other_id in result if other_id != entry_id]
        assert len(copies) == 1
        assert result[copies[0]] == ("导入修改标题", "导入修改正文")
        repeated = target.inspect_import(file, EXPORT_PASSWORD)
        assert repeated.counts["duplicate"] == 1
        target.commit_import(repeated, {})
        assert contents(target) == result


def test_possible_duplicate_title_preserves_both_and_exact_text(tmp_path):
    source, target = initialized_pair(tmp_path)
    source.put_entry("重复的虚构标题", "AbC \r\n")
    local_id = target.put_entry("重复的虚构标题", "abc\n")
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    assert plan.counts["possible_duplicate"] == 1
    item = next(item for item in plan.items if item.kind == "possible_duplicate")
    target.commit_import(plan, {item.incoming.id: "both"})
    result = contents(target)
    assert len(result) == 2
    assert result[local_id] == ("重复的虚构标题", "abc\n")
    assert set(result.values()) == {("重复的虚构标题", "AbC \r\n"), ("重复的虚构标题", "abc\n")}


def test_partial_import_keeps_other_local_entries(tmp_path):
    source, target = initialized_pair(tmp_path)
    included = source.put_entry("导出条目", "正文甲")
    source.put_entry("不导出条目", "正文乙")
    local_id = target.put_entry("目标已有条目", "原有正文")
    plan = target.inspect_import(export(source, tmp_path, [included]), EXPORT_PASSWORD)
    target.commit_import(plan, {})
    assert contents(target) == {local_id: ("目标已有条目", "原有正文"), included: ("导出条目", "正文甲")}


def write_fake_import(tmp_path, entries):
    """按给定编号与顺序生成合法导出文件，内容全部虚构。"""
    data, _ = core._new_data(EXPORT_PASSWORD, entries, core.EXPORT_FORMAT)
    file = tmp_path / "batch.lvexport"
    file.write_bytes(core._serialize(data))
    return file


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("choice", ["local", "import", "both"])
def test_batch_overwrite_rechecks_old_content_duplicate(vault, tmp_path, reverse, choice):
    old = ("旧虚构标题", "  旧正文 AbC\r\n\t尾部  ")
    new = ("新虚构标题", "新正文\n  ")
    local_id = vault.put_entry(*old)
    copy_id = "00000000-0000-0000-0000-000000000002"
    incoming = [core.Entry(local_id, *new), core.Entry(copy_id, *old)]
    file = write_fake_import(tmp_path, incoming[::-1] if reverse else incoming)
    plan = vault.inspect_import(file, EXPORT_PASSWORD)
    assert plan.counts["conflict"] == plan.counts["duplicate"] == 1
    original = vault.path.read_bytes()
    vault.commit_import(plan, {local_id: choice})
    expected = {old} if choice == "local" else {old, new}
    result = contents(vault)
    assert set(result.values()) == expected
    assert len(result) == len(expected)
    assert result[local_id] == (new if choice == "import" else old)
    if choice == "import":
        assert result[copy_id] == old
    if choice == "local":
        assert vault.path.read_bytes() == original
    for _ in range(2):
        repeated = vault.inspect_import(file, EXPORT_PASSWORD)
        choices = {item.incoming.id: choice for item in repeated.items if item.kind == "conflict"}
        vault.commit_import(repeated, choices)
        assert contents(vault) == result


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("choice", ["local", "import", "both"])
def test_batch_incoming_duplicate_does_not_hide_id_conflict(vault, tmp_path, reverse, choice):
    old = ("虚构旧条目", "旧内容")
    new = ("虚构更新条目", "  新内容\r\nAbC  ")
    local_id = vault.put_entry(*old)
    copy_id = "00000000-0000-0000-0000-000000000003"
    incoming = [core.Entry(local_id, *new), core.Entry(copy_id, *new)]
    file = write_fake_import(tmp_path, incoming[::-1] if reverse else incoming)
    plan = vault.inspect_import(file, EXPORT_PASSWORD)
    assert next(item for item in plan.items if item.incoming.id == local_id).kind == "conflict"
    vault.commit_import(plan, {local_id: choice})
    expected = {new} if choice == "import" else {old, new}
    result = contents(vault)
    assert set(result.values()) == expected
    assert len(result) == len(expected)
    if choice != "import":
        assert result[local_id] == old
    repeated = vault.inspect_import(file, EXPORT_PASSWORD)
    vault.commit_import(repeated, {item.incoming.id: choice for item in repeated.items if item.kind == "conflict"})
    assert contents(vault) == result


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_duplicate_with_colliding_id_preserves_unselected_local(vault, tmp_path, reverse):
    original = ("虚构编号甲", "甲的原正文")
    moved = ("虚构旧内容", "乙的原正文")
    updated = ("虚构新内容", "乙的新正文")
    first_id = vault.put_entry(*original)
    second_id = vault.put_entry(*moved)
    incoming = [core.Entry(first_id, *moved), core.Entry(second_id, *updated)]
    file = write_fake_import(tmp_path, incoming[::-1] if reverse else incoming)
    plan = vault.inspect_import(file, EXPORT_PASSWORD)
    assert next(item for item in plan.items if item.incoming.id == first_id).kind == "duplicate"
    vault.commit_import(plan, {second_id: "import"})
    result = contents(vault)
    assert result[first_id] == original
    assert result[second_id] == updated
    assert set(result.values()) == {original, moved, updated}
    assert len(result) == 3
    repeated = vault.inspect_import(file, EXPORT_PASSWORD)
    vault.commit_import(repeated, {})
    assert contents(vault) == result


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_two_overwrites_deduplicate_after_all_choices(vault, tmp_path, reverse):
    first_id = vault.put_entry("虚构旧甲", "甲旧正文")
    second_id = vault.put_entry("虚构旧乙", "乙旧正文")
    final = ("虚构共同新标题", "共同新正文")
    incoming = [core.Entry(first_id, *final), core.Entry(second_id, *final)]
    file = write_fake_import(tmp_path, incoming[::-1] if reverse else incoming)
    plan = vault.inspect_import(file, EXPORT_PASSWORD)
    assert plan.counts["conflict"] == 2
    vault.commit_import(plan, {first_id: "import", second_id: "import"})
    assert contents(vault) == {first_id: final}
    result = contents(vault)
    vault.commit_import(vault.inspect_import(file, EXPORT_PASSWORD), {})
    assert contents(vault) == result


@pytest.mark.parametrize("failure", ["cancel", "replace"])
def test_batch_overwrite_cancel_or_save_failure_is_atomic(vault, tmp_path, monkeypatch, failure):
    old = ("虚构原标题", "原正文")
    local_id = vault.put_entry(*old)
    unrelated_id = vault.put_entry("虚构无关条目", "必须保留")
    file = write_fake_import(tmp_path, [
        core.Entry(local_id, "虚构新标题", "新正文"),
        core.Entry("00000000-0000-0000-0000-000000000004", *old),
    ])
    original = vault.path.read_bytes()
    before = contents(vault)
    plan = vault.inspect_import(file, EXPORT_PASSWORD)
    if failure == "replace":
        replace = os.replace

        def fail_final(source, destination):
            if Path(destination) == vault.path:
                raise PermissionError("模拟整批导入最终替换失败")
            return replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", fail_final)
            with pytest.raises(VaultError):
                vault.commit_import(plan, {local_id: "import"})
    assert vault.path.read_bytes() == original
    assert contents(vault) == before
    vault.commit_import(plan, {local_id: "import"})
    assert contents(vault)[unrelated_id] == before[unrelated_id]
    assert set(contents(vault).values()) == {old, ("虚构新标题", "新正文"), before[unrelated_id]}


def test_partial_import_preserves_preexisting_local_duplicate_entries(vault, tmp_path):
    first_id = vault.put_entry("虚构既有同文", "原先就相同的内容")
    second_id = vault.put_entry("虚构既有同文", "原先就相同的内容")
    new_id = "00000000-0000-0000-0000-000000000005"
    file = write_fake_import(tmp_path, [core.Entry(new_id, "虚构新增", "新增正文")])
    vault.commit_import(vault.inspect_import(file, EXPORT_PASSWORD), {})
    assert contents(vault) == {
        first_id: ("虚构既有同文", "原先就相同的内容"),
        second_id: ("虚构既有同文", "原先就相同的内容"),
        new_id: ("虚构新增", "新增正文"),
    }


def test_import_plan_expires_after_local_change(tmp_path):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构待导入", "虚构正文")
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    local_id = target.put_entry("计划后添加", "必须保留")
    original = target.path.read_bytes()
    with pytest.raises(VaultError):
        target.commit_import(plan, {})
    assert target.path.read_bytes() == original
    assert contents(target) == {local_id: ("计划后添加", "必须保留")}


def test_missing_conflict_choice_does_not_modify(tmp_path):
    source, target = initialized_pair(tmp_path)
    entry_id = source.put_entry("虚构条目", "来源正文")
    file = export(source, tmp_path)
    target.commit_import(target.inspect_import(file, EXPORT_PASSWORD), {})
    target.put_entry("虚构条目", "本地正文", entry_id=entry_id)
    plan = target.inspect_import(file, EXPORT_PASSWORD)
    original = target.path.read_bytes()
    with pytest.raises(VaultError):
        target.commit_import(plan, {})
    assert target.path.read_bytes() == original
    assert target.get_entry(entry_id).body == "本地正文"


def test_password_change_preserves_entries_and_rejects_old(vault, samples):
    add_samples(vault, samples)
    before = contents(vault)
    vault.change_password(OTHER_MASTER)
    assert contents(vault) == before
    reopened = Vault(vault.path)
    with pytest.raises(VaultError):
        reopened.unlock(MASTER)
    reopened.unlock(OTHER_MASTER)
    assert contents(reopened) == before


def test_backup_restore_complete_vault(tmp_path, samples):
    source, target = initialized_pair(tmp_path)
    add_samples(source, samples)
    source.set_idle_minutes(17)
    before = contents(source)
    backup = tmp_path / "complete.lvbackup"
    source.backup_file(backup)
    assert MASTER not in backup.read_text(encoding="utf-8")
    target.put_entry("将被完整恢复替换的虚构条目", "虚构正文")
    target.restore_backup(backup, MASTER)
    target.lock()
    target.unlock(MASTER)
    assert contents(target) == before
    assert target.idle_minutes == 17
    reopened = Vault(target.path)
    reopened.unlock(MASTER)
    assert contents(reopened) == before


@pytest.mark.parametrize("failure", ["password", "malformed", "ciphertext"])
def test_bad_backup_restore_does_not_modify(tmp_path, failure):
    source, target = initialized_pair(tmp_path)
    source.put_entry("备份中的虚构条目", "备份中的正文")
    local_id = target.put_entry("本地条目", "必须保留")
    backup = tmp_path / "complete.lvbackup"
    source.backup_file(backup)
    if failure == "malformed":
        backup.write_text("{损坏备份", encoding="utf-8")
    elif failure == "ciphertext":
        corrupt_ciphertext(backup)
    original = target.path.read_bytes()
    with pytest.raises(VaultError):
        target.restore_backup(backup, WRONG if failure == "password" else MASTER)
    assert target.path.read_bytes() == original
    assert contents(target) == {local_id: ("本地条目", "必须保留")}


def test_concurrent_instance_cannot_overwrite_newer_save(vault):
    entry_id = vault.put_entry("虚构原条目", "原正文")
    other = Vault(vault.path)
    other.unlock(MASTER)
    vault.put_entry("虚构最新条目", "最新正文", entry_id=entry_id)
    latest = vault.path.read_bytes()
    with pytest.raises(VaultError):
        other.put_entry("陈旧实例的标题", "不得覆盖最新正文", entry_id=entry_id)
    assert vault.path.read_bytes() == latest
    reopened = Vault(vault.path)
    reopened.unlock(MASTER)
    assert reopened.get_entry(entry_id).body == "最新正文"


def test_create_never_overwrites_existing_vault(vault):
    vault.put_entry("已经存在的虚构条目", "必须保留")
    original = vault.path.read_bytes()
    with pytest.raises(VaultError):
        vault.create(OTHER_MASTER)
    assert vault.path.read_bytes() == original


@pytest.mark.parametrize("title", ["", "   ", "\r\n\t"])
def test_blank_title_rejected(vault, title):
    original = vault.path.read_bytes()
    with pytest.raises(VaultError):
        vault.put_entry(title, "虚构正文")
    assert vault.path.read_bytes() == original


def test_idle_setting_persists_and_invalid_values_rejected(vault):
    vault.set_idle_minutes(0)
    assert vault.idle_minutes == 0
    vault.set_idle_minutes(11)
    assert Vault(vault.path).idle_minutes == 11
    original = vault.path.read_bytes()
    for value in [-1, 1.5, "10", True]:
        with pytest.raises(VaultError):
            vault.set_idle_minutes(value)
        assert vault.path.read_bytes() == original


def test_locked_modifications_and_unknown_ids_rejected(vault, tmp_path):
    entry_id = vault.put_entry("虚构条目", "虚构正文")
    original = vault.path.read_bytes()
    for call in [lambda: vault.get_entry("不存在编号"), lambda: vault.delete("不存在编号")]:
        with pytest.raises(VaultError):
            call()
    vault.lock()
    for call in [
        lambda: vault.put_entry("禁止保存", "禁止保存"),
        lambda: vault.delete(entry_id),
        lambda: vault.change_password(OTHER_MASTER),
        lambda: vault.export_file(tmp_path / "locked.lvexport", EXPORT_PASSWORD),
    ]:
        with pytest.raises(VaultError):
            call()
    assert vault.path.read_bytes() == original


def test_export_unknown_selection_does_not_create_file(vault, tmp_path):
    vault.put_entry("虚构条目", "虚构正文")
    output = tmp_path / "unknown.lvexport"
    with pytest.raises(VaultError):
        vault.export_file(output, EXPORT_PASSWORD, ids=["不存在编号"])
    assert not output.exists()


@pytest.mark.parametrize("password", ["", "短密码", "x" * 1025])
def test_password_limits_reject_without_creating_vault(tmp_path, password):
    path = tmp_path / "invalid.json"
    instance = Vault(path)
    with pytest.raises(VaultError):
        instance.create(password)
    assert not path.exists()
    assert not instance.is_initialized


def test_title_body_and_idle_upper_limits(vault):
    original = vault.path.read_bytes()
    for call in [
        lambda: vault.put_entry("标" * 201, "虚构正文"),
        lambda: vault.put_entry("虚构过长正文", "x" * (8 * 1024 * 1024 + 1)),
        lambda: vault.set_idle_minutes(121),
    ]:
        with pytest.raises(VaultError):
            call()
        assert vault.path.read_bytes() == original


def test_titles_preserve_spaces_and_content_comparison_is_exact(tmp_path):
    source, target = initialized_pair(tmp_path)
    source.put_entry("  虚构标题  ", "AbC\r\n  ")
    target.put_entry("虚构标题", "AbC\r\n  ")
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    assert plan.counts["new"] == 1
    target.commit_import(plan, {})
    assert {entry.title for entry_id, _ in target.list_titles() for entry in [target.get_entry(entry_id)]} == {
        "  虚构标题  ", "虚构标题"
    }


def test_duplicate_content_within_export_imports_once(tmp_path):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构相同内容", "精确相同的虚构正文")
    source.put_entry("虚构相同内容", "精确相同的虚构正文")
    file = export(source, tmp_path)
    target.commit_import(target.inspect_import(file, EXPORT_PASSWORD), {})
    assert len(target.list_titles()) == 1
    target.commit_import(target.inspect_import(file, EXPORT_PASSWORD), {})
    assert len(target.list_titles()) == 1


@pytest.mark.parametrize("remove_all", [False, True])
def test_deleted_directory_entries_detected_by_authentication(vault, remove_all):
    vault.put_entry("虚构条目甲", "甲正文")
    vault.put_entry("虚构条目乙", "乙正文")
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    data["entries"] = [] if remove_all else data["entries"][1:]
    vault.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    damaged = vault.path.read_bytes()
    with pytest.raises(VaultError):
        Vault(vault.path).unlock(MASTER)
    assert vault.path.read_bytes() == damaged


def test_hostile_kdf_parameters_rejected_before_derivation(vault, monkeypatch):
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    data["kdf"]["n"] = 2**40
    vault.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def forbidden_derivation(*args, **kwargs):
        pytest.fail("未经校验的异常参数不应进入密码派生")

    monkeypatch.setattr(core, "_derive", forbidden_derivation)
    with pytest.raises(VaultError):
        Vault(vault.path)


def test_duplicate_json_fields_rejected(vault):
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    key = next(iter(data))
    duplicated = (
        "{" + json.dumps(key) + ":" + json.dumps(data[key], ensure_ascii=False) + ","
        + json.dumps(data, ensure_ascii=False)[1:]
    )
    vault.path.write_text(duplicated, encoding="utf-8")
    damaged = vault.path.read_bytes()
    with pytest.raises(VaultError):
        Vault(vault.path)
    assert vault.path.read_bytes() == damaged


@pytest.mark.parametrize("tampering", ["items", "counts"])
def test_import_preview_tampering_cannot_commit(tmp_path, tampering):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构导入条目", "虚构正文")
    plan = target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD)
    if tampering == "items":
        plan.items.clear()
    else:
        plan.counts["new"] = 999
    original = target.path.read_bytes()
    with pytest.raises(VaultError):
        target.commit_import(plan, {})
    assert target.path.read_bytes() == original
    assert target.list_titles() == []


def test_sealed_unsaved_draft_survives_lock_and_rejects_tampering(vault):
    draft = core.Entry("", "", "  虚构未保存草稿\r\nAbC \t\n\n  ")
    sealed = vault.seal_draft(draft)
    assert draft.body.encode("utf-8") not in sealed
    assert vault.open_draft(sealed) == draft
    vault.lock()
    with pytest.raises(VaultError):
        vault.open_draft(sealed)
    with pytest.raises(VaultError):
        vault.seal_draft(draft)
    vault.unlock(MASTER)
    assert vault.open_draft(sealed) == draft
    damaged = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(VaultError):
        vault.open_draft(damaged)
    assert vault.open_draft(sealed) == draft


def test_same_instance_concurrent_threads_preserve_both_new_entries(vault):
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(vault.put_entry, f"虚构线程条目{i}", f"虚构正文{i}") for i in range(2)]
        ids = [future.result() for future in futures]
    assert len(set(ids)) == 2
    expected = {ids[i]: (f"虚构线程条目{i}", f"虚构正文{i}") for i in range(2)}
    assert contents(vault) == expected
    reopened = Vault(vault.path)
    reopened.unlock(MASTER)
    assert contents(reopened) == expected


def test_oversized_unsaved_draft_can_lock_without_losing_text(vault):
    draft = core.Entry("", "标" * 201, "x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(VaultError):
        vault.put_entry(draft.title, draft.body)
    sealed = vault.seal_draft(draft)
    vault.lock()
    vault.unlock(MASTER)
    assert vault.open_draft(sealed) == draft


def test_damaged_vault_restore_recovers_and_keeps_original_backup(tmp_path):
    source = Vault(tmp_path / "source.json")
    source.create(MASTER)
    entry_id = source.put_entry("虚构恢复条目", "虚构恢复正文\r\n  AbC  ")
    backup = tmp_path / "source.lvbackup"
    source.backup_file(backup)
    damaged_path = tmp_path / "damaged.json"
    damaged_bytes = b"{broken-original-vault"
    damaged_path.write_bytes(damaged_bytes)
    recovery = Vault.for_restore(damaged_path)
    recovery.restore_backup(backup, MASTER)
    assert recovery.is_unlocked
    assert recovery.get_entry(entry_id).body == "虚构恢复正文\r\n  AbC  "
    assert not Path(str(damaged_path) + ".bak").exists()
    new_id = recovery.put_entry("恢复后的新虚构条目", "新正文")
    reopened = Vault(damaged_path)
    reopened.unlock(MASTER)
    assert reopened.get_entry(new_id).body == "新正文"


@pytest.mark.parametrize("failure", ["password", "backup"])
def test_damaged_vault_recovery_failure_keeps_original(tmp_path, failure):
    source = Vault(tmp_path / "source.json")
    source.create(MASTER)
    source.put_entry("虚构条目", "虚构正文")
    backup = tmp_path / "source.lvbackup"
    source.backup_file(backup)
    if failure == "backup":
        backup.write_bytes(b"damaged-backup")
    damaged_path = tmp_path / "damaged.json"
    original = b"unreadable-original-vault"
    damaged_path.write_bytes(original)
    recovery = Vault.for_restore(damaged_path)
    with pytest.raises(VaultError):
        recovery.restore_backup(backup, WRONG if failure == "password" else MASTER)
    assert damaged_path.read_bytes() == original


def test_recovery_mode_forbids_overwriting_original_with_new_vault(tmp_path):
    path = tmp_path / "damaged.json"
    original = b"damaged-original-vault"
    path.write_bytes(original)
    recovery = Vault.for_restore(path)
    for call in [
        lambda: recovery.create(MASTER),
        lambda: recovery.unlock(MASTER),
        lambda: recovery.list_titles(),
        lambda: recovery.put_entry("禁止写入", "不得覆盖"),
        lambda: recovery.backup_file(tmp_path / "forbidden.lvbackup"),
    ]:
        with pytest.raises(VaultError):
            call()
    assert path.read_bytes() == original


def prepare_auto_backup_recovery(tmp_path):
    source = Vault(tmp_path / "source.lvault")
    source.create(MASTER)
    entry_id = source.put_entry("虚构自动备份条目", "虚构备份正文\r\n  AbC  ")
    damaged_path = tmp_path / "vault.lvault"
    original = b"damaged-fictional-main-vault"
    damaged_path.write_bytes(original)
    backup = Path(str(damaged_path) + ".bak")
    source.backup_file(backup)
    return Vault.for_restore(damaged_path), backup, original, entry_id


@pytest.mark.parametrize("source_form", ["direct", "normalized_alias", "hardlink"])
def test_auto_backup_final_replace_failure_keeps_source_and_allows_retry(
        tmp_path, monkeypatch, source_form):
    recovery, backup, original, entry_id = prepare_auto_backup_recovery(tmp_path)
    source = backup
    if source_form == "normalized_alias":
        alias_directory = tmp_path / "路径别名"
        alias_directory.mkdir()
        source = alias_directory / ".." / backup.name
    elif source_form == "hardlink":
        source = tmp_path / "硬链接恢复来源.lvbackup"
        os.link(backup, source)
    backup_bytes = backup.read_bytes()
    real_replace = os.replace

    def fail_final_replace(temporary, destination):
        if Path(destination) == recovery.path:
            raise PermissionError("模拟最终替换主文件失败")
        return real_replace(temporary, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_final_replace)
        with pytest.raises(VaultError, match="保存失败"):
            recovery.restore_backup(source, MASTER)

    assert recovery.path.read_bytes() == original
    assert backup.read_bytes() == backup_bytes
    assert source.read_bytes() == backup_bytes
    assert not recovery.is_initialized and not recovery.is_unlocked
    readable_backup = Vault(backup)
    readable_backup.unlock(MASTER)
    assert readable_backup.get_entry(entry_id).body == "虚构备份正文\r\n  AbC  "
    assert not list(tmp_path.glob(".*.tmp"))

    recovery.restore_backup(source, MASTER)
    assert recovery.path.read_bytes() == backup_bytes
    assert backup.read_bytes() == backup_bytes
    assert recovery.get_entry(entry_id).body == "虚构备份正文\r\n  AbC  "
    assert not list(tmp_path.glob("vault.lvault.before-restore-*.bak"))


@pytest.mark.parametrize("failure", ["password", "malformed", "ciphertext"])
def test_auto_backup_invalid_restore_keeps_main_and_source(tmp_path, failure):
    recovery, backup, original, _ = prepare_auto_backup_recovery(tmp_path)
    if failure == "malformed":
        backup.write_bytes(b"damaged-fictional-backup")
    elif failure == "ciphertext":
        corrupt_ciphertext(backup)
    backup_bytes = backup.read_bytes()
    with pytest.raises(VaultError):
        recovery.restore_backup(backup, WRONG if failure == "password" else MASTER)
    assert recovery.path.read_bytes() == original
    assert backup.read_bytes() == backup_bytes
    assert not recovery.is_initialized and not recovery.is_unlocked
    assert not list(tmp_path.glob("vault.lvault.before-restore-*.bak"))


def test_auto_backup_cancel_before_commit_keeps_main_and_source(tmp_path):
    recovery, backup, original, entry_id = prepare_auto_backup_recovery(tmp_path)
    backup_bytes = backup.read_bytes()
    # 核心层取消恢复意味着不提交；即使先验证过备份，也不写入任何文件。
    readable_backup = Vault(backup)
    readable_backup.unlock(MASTER)
    assert readable_backup.get_entry(entry_id).body == "虚构备份正文\r\n  AbC  "
    assert recovery.path.read_bytes() == original
    assert backup.read_bytes() == backup_bytes
    assert not recovery.is_initialized and not recovery.is_unlocked


def test_restore_from_other_location_keeps_source_and_existing_legacy_backup(tmp_path):
    recovery, automatic_backup, original, entry_id = prepare_auto_backup_recovery(tmp_path)
    backup = tmp_path / "其他目录" / "完整虚构备份.lvbackup"
    backup.parent.mkdir()
    backup.write_bytes(automatic_backup.read_bytes())
    source_bytes = backup.read_bytes()
    recovery.restore_backup(backup, MASTER)
    assert backup.read_bytes() == source_bytes
    assert automatic_backup.read_bytes() == source_bytes
    assert recovery.get_entry(entry_id).body == "虚构备份正文\r\n  AbC  "
    assert not list(tmp_path.glob("vault.lvault.before-restore-*.bak"))


def test_ordinary_save_keeps_existing_legacy_backup_unchanged(vault):
    entry_id = vault.put_entry("虚构原始条目", "原正文")
    previous_bytes = vault.path.read_bytes()
    backup = Path(str(vault.path) + ".bak")
    backup.write_bytes(previous_bytes)
    vault.put_entry("虚构更新条目", "新正文", entry_id=entry_id)
    assert backup.read_bytes() == previous_bytes
    readable_backup = Vault(backup)
    readable_backup.unlock(MASTER)
    assert readable_backup.get_entry(entry_id).body == "原正文"
    assert vault.get_entry(entry_id).body == "新正文"
    assert not list(vault.path.parent.glob("*.before-restore-*.bak"))


def write_legacy_file(path, password="Old!1234", *, file_format=core.VAULT_FORMAT):
    """使用首版固定参数构造虚构旧文件，不借用新密码创建入口。"""
    salt = b"fictional-salt!!"
    assert len(salt) == 16
    kdf = {"name": "scrypt", "salt": core._encode(salt), "n": 32768, "r": 8, "p": 1}
    key = core.Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode("utf-8"))
    entry = core.Entry("00000000-0000-0000-0000-000000000008", "旧版虚构条目", "  旧正文 AbC\r\n秘密仅为测试  ")
    nonce = os.urandom(12)
    data = {
        "format": file_format, "version": 1, "kdf": kdf,
        "check": {"nonce": core._encode(nonce), "ciphertext": core._encode(core.AESGCM(key).encrypt(
            nonce, core._CHECK_PLAIN, core._CHECK_AAD + file_format.encode("ascii")))},
        "entries": [core._encrypt_entry(entry, key)],
    }
    if file_format == core.VAULT_FORMAT:
        data["idle_minutes"] = 17
    core._authenticate(data, key)
    path.write_bytes(core._serialize(data))
    return entry


@pytest.mark.parametrize("password", [
    "", "12345678", "x" * 15, " " * 16, "\t\n" * 8,
    "Password123456789", "Password!Password!", "qwertyuiop12345678",
    "1111111111111111", "abcd" * 4, "1234567890123456",
    "123456789012345!", "aaaaaaaaaaaaaaa!",
    "correct horse battery staple", "x" * 1025,
])
def test_new_password_rules_are_shared_and_never_write(vault, tmp_path, password):
    original = vault.path.read_bytes()
    create_path = tmp_path / "rejected-new.lvault"
    output = tmp_path / "rejected-export.lvexport"
    for action in [
        lambda: Vault(create_path).create(password),
        lambda: vault.change_password(password),
        lambda: vault.export_file(output, password),
    ]:
        with pytest.raises(VaultError):
            action()
    assert vault.path.read_bytes() == original
    assert not create_path.exists() and not output.exists()
    assert vault.is_unlocked


@pytest.mark.parametrize("password", [
    "山谷里的银杏树在深秋仍然记得清晨", "8274601953862047", "^%&@!~:;{}[]()+-=",
    "  虚构长口令-MountainRiver-GREEN-2026  ",
    "虚构长口令-" + "山谷晨光银杏的回声" * 114,
], ids=["中文口令", "纯数字口令", "符号口令", "保留空格及大小写", "长度1024"])
def test_new_passwords_allow_long_phrases_and_preserve_exact_input(tmp_path, password):
    # 保持上限测试精确为 1024 字符；不要求混合字符种类。
    if len(password) > 1000:
        password = password[:1024]
        assert len(password) == 1024
    path = tmp_path / "accepted-new.lvault"
    instance = Vault(path)
    instance.create(password)
    instance.lock()
    reopened = Vault(path)
    reopened.unlock(password)
    assert reopened.is_unlocked
    modified = password.strip() if password != password.strip() else password.swapcase()
    if modified != password:
        with pytest.raises(VaultError):
            Vault(path).unlock(modified)


def test_new_create_change_and_export_write_strengthened_parameters(vault, tmp_path):
    def parameters(path):
        kdf = json.loads(path.read_text(encoding="utf-8"))["kdf"]
        return (kdf["n"], kdf["r"], kdf["p"]), kdf["salt"]

    first, first_salt = parameters(vault.path)
    assert first == (131072, 8, 1)
    entry_id = vault.put_entry("虚构新参数条目", "正文完整保留")
    vault.change_password(OTHER_MASTER)
    changed, changed_salt = parameters(vault.path)
    assert changed == first and changed_salt != first_salt
    output = tmp_path / "new.lvexport"
    vault.export_file(output, EXPORT_PASSWORD)
    exported, export_salt = parameters(output)
    assert exported == first and export_salt not in {first_salt, changed_salt}
    assert vault.get_entry(entry_id).body == "正文完整保留"


@pytest.mark.parametrize("legacy_password", ["Old!1234", "12345678", " " * 8])
def test_legacy_short_and_weak_passwords_remain_readable(tmp_path, legacy_password):
    path = tmp_path / "old.lvault"
    entry = write_legacy_file(path, legacy_password)
    instance = Vault(path)
    instance.unlock(legacy_password)
    assert instance.get_entry(entry.id) == entry
    instance.put_entry("旧库普通编辑", "仍可用旧密码读取")
    assert json.loads(path.read_text(encoding="utf-8"))["kdf"]["n"] == 32768
    reopened = Vault(path)
    reopened.unlock(legacy_password)
    assert reopened.get_entry(entry.id) == entry


def test_legacy_export_and_complete_backup_use_original_eight_character_password(vault, tmp_path):
    password = "Old!1234"
    export_path = tmp_path / "old.lvexport"
    entry = write_legacy_file(export_path, password, file_format=core.EXPORT_FORMAT)
    vault.commit_import(vault.inspect_import(export_path, password), {})
    assert vault.get_entry(entry.id) == entry
    backup = tmp_path / "old.lvbackup"
    write_legacy_file(backup, password)
    backup_bytes = backup.read_bytes()
    vault.restore_backup(backup, password)
    assert vault.get_entry(entry.id) == entry and vault.idle_minutes == 17
    vault.lock()
    vault.unlock(password)
    assert backup.read_bytes() == backup_bytes


@pytest.mark.parametrize("failure", ["weak", "replace", "none"])
def test_legacy_password_upgrade_is_atomic_and_preserves_contents(tmp_path, monkeypatch, failure):
    path = tmp_path / "old.lvault"
    entry = write_legacy_file(path)
    original = path.read_bytes()
    instance = Vault(path)
    instance.unlock("Old!1234")
    if failure == "weak":
        with pytest.raises(VaultError):
            instance.change_password("1234567890123456")
    elif failure == "replace":
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("模拟迁移提交失败")))
            with pytest.raises(VaultError):
                instance.change_password(OTHER_MASTER)
    if failure != "none":
        assert path.read_bytes() == original
        assert instance.get_entry(entry.id) == entry
        Vault(path).unlock("Old!1234")
    instance.change_password(OTHER_MASTER)
    assert json.loads(path.read_text(encoding="utf-8"))["kdf"]["n"] == 131072
    assert instance.get_entry(entry.id) == entry and instance.idle_minutes == 17
    with pytest.raises(VaultError):
        Vault(path).unlock("Old!1234")
    reopened = Vault(path)
    reopened.unlock(OTHER_MASTER)
    assert reopened.get_entry(entry.id) == entry
    assert not Path(str(path) + ".bak").exists()


@pytest.mark.parametrize("parameters", [
    (65536, 8, 1), (131072, 9, 1), (32768, 8, 2), (2**40, 8, 1),
    (True, 8, 1), (131072, 8.0, 1), (131072, 8, "1"),
])
def test_only_two_explicit_kdf_configurations_can_reach_derivation(vault, monkeypatch, parameters):
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    data["kdf"].update(zip(("n", "r", "p"), parameters))
    vault.path.write_bytes(core._serialize(data))

    def forbidden(*args, **kwargs):
        pytest.fail("不受支持的参数不得触发实际密码派生")

    monkeypatch.setattr(core, "Scrypt", forbidden)
    with pytest.raises(VaultError):
        Vault(vault.path)
    with pytest.raises(VaultError):
        core._derive(MASTER, data["kdf"])


def test_all_current_content_operations_leave_no_automatic_history(tmp_path):
    source, target = initialized_pair(tmp_path)
    source.put_entry("虚构导入条目", "导入正文")
    entry_id = target.put_entry("虚构初始条目", "初始正文")
    target.put_entry("虚构编辑条目", "编辑正文", entry_id=entry_id)
    target.delete(entry_id)
    target.commit_import(target.inspect_import(export(source, tmp_path), EXPORT_PASSWORD), {})
    target.set_idle_minutes(12)
    target.change_password(MASTER)
    backup = tmp_path / "manual.lvbackup"
    target.backup_file(backup)
    target.restore_backup(backup, MASTER)
    target.backup_file(backup)
    source.export_file(tmp_path / "transfer.lvexport", EXPORT_PASSWORD)
    reopened = Vault(target.path)
    reopened.unlock(MASTER)
    assert entry_id not in dict(reopened.list_titles())
    assert contents(reopened) == contents(source)
    assert not list(tmp_path.rglob("*.bak"))
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_creation_does_not_commit_memory_or_leave_encrypted_temporary(tmp_path, monkeypatch, failure):
    path = tmp_path / "new.lvault"
    instance = Vault(path)

    def fail(*args, **kwargs):
        raise OSError("模拟首次保存失败")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync" if failure == "write" else "replace", fail)
        with pytest.raises(VaultError):
            instance.create(MASTER)
    assert not path.exists()
    assert not instance.is_initialized and not instance.is_unlocked
    assert not list(tmp_path.rglob("*.bak"))
    assert not list(tmp_path.rglob("*.tmp"))
    instance.create(MASTER)
    assert instance.is_initialized and instance.is_unlocked


@pytest.mark.parametrize("operation", ["edit", "settings", "backup", "export", "restore"])
@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_writes_preserve_original_targets_and_restore_source(vault, tmp_path, monkeypatch, operation, failure):
    entry_id = vault.put_entry("虚构原条目", "必须保留的正文")
    backup = tmp_path / "manual.lvbackup"
    output = tmp_path / "manual.lvexport"
    vault.backup_file(backup)
    vault.export_file(output, EXPORT_PASSWORD)
    originals = {path: path.read_bytes() for path in (vault.path, backup, output)}
    before = contents(vault)
    action = {
        "edit": lambda: vault.put_entry("未提交标题", "未提交正文", entry_id=entry_id),
        "settings": lambda: vault.set_idle_minutes(19),
        "backup": lambda: vault.backup_file(backup),
        "export": lambda: vault.export_file(output, OTHER_MASTER),
        "restore": lambda: vault.restore_backup(backup, MASTER),
    }[operation]

    def fail(*args, **kwargs):
        raise OSError("模拟磁盘写入或提交失败")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync" if failure == "write" else "replace", fail)
        with pytest.raises(VaultError):
            action()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert contents(vault) == before and vault.idle_minutes == 5
    assert not list(tmp_path.rglob("*.bak"))
    assert not list(tmp_path.rglob("*.tmp"))
