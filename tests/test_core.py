"""使用完全虚构的数据验证保险库保存、密码保护和跨电脑合并。"""

import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import local_vault.core as core
from local_vault.core import Vault, VaultError


MASTER = "虚构主密码-2026-Only!"
OTHER_MASTER = "另一台虚构主密码-2026!"
EXPORT_PASSWORD = "虚构导出密码-2026!"
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
    assert Path(str(damaged_path) + ".bak").read_bytes() == damaged_bytes
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
