"""本地密匣的数据、加密与合并核心。

标题以明文保存，正文使用 scrypt 派生的 AES-256-GCM 密钥加密。
另外的 GCM 认证标签覆盖整个文件目录，防止条目被替换、删去或改名。
所有写入先写同目录临时文件，再以系统原子替换提交。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_TITLE_LENGTH = 200
MIN_PASSWORD_LENGTH = 16
LEGACY_MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SUPPORTED_SCRYPT_PARAMETERS = frozenset({(32768, 8, 1), (SCRYPT_N, SCRYPT_R, SCRYPT_P)})
VAULT_FORMAT = "local-vault"
EXPORT_FORMAT = "local-vault-export"
FORMAT_VERSION = 1
_CHECK_PLAIN = "本地密匣密码验证：第一版".encode("utf-8")
_BODY_AAD = b"local-vault:body:v1\x00"
_CHECK_AAD = b"local-vault:password:v1\x00"
_DIRECTORY_AAD = b"local-vault:directory:v1\x00"
_DRAFT_AAD = b"local-vault:draft:v1\x00"
_COMMON_PASSWORD_WORDS = frozenset({
    "password", "passw0rd", "qwerty", "qwertyuiop", "asdfgh", "asdfghjkl", "zxcvbnm",
    "letmein", "welcome", "admin", "administrator", "iloveyou", "changeme", "monkey",
    "dragon", "football", "baseball", "trustno", "abc", "abcdef", "abcdefg",
    "密码", "主密码", "测试密码", "默认密码", "我的密码",
    "correcthorsebatterystaple",
})


class VaultError(Exception):
    """适合直接向用户显示的中文错误。"""


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    body: str


@dataclass(frozen=True)
class MergeItem:
    incoming: Entry
    kind: str
    local: Entry | None = None


@dataclass
class ImportPlan:
    items: list[MergeItem]
    counts: dict[str, int]
    _token: str = field(default="", repr=False)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any, *, size: int | None = None, maximum: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise VaultError("文件格式错误：加密字段必须是文本。")
    if maximum is not None and len(value) > ((maximum + 2) // 3) * 4:
        raise VaultError("文件格式错误：加密字段过大。")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise VaultError("文件格式错误：加密字段编码无效。") from exc
    if _encode(result) != value:
        raise VaultError("文件格式错误：加密字段编码不规范。")
    if size is not None and len(result) != size:
        raise VaultError("文件格式错误：加密字段长度无效。")
    if maximum is not None and len(result) > maximum:
        raise VaultError("文件格式错误：加密字段过大。")
    return result


def _exact_keys(value: Any, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise VaultError("文件格式错误：字段缺失或含有不支持的字段。")


def _valid_id(value: Any, *, allow_empty: bool = False) -> None:
    if allow_empty and value == "":
        return
    if not isinstance(value, str):
        raise VaultError("条目的唯一编号无效。")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise VaultError("条目的唯一编号无效。") from exc
    if str(parsed) != value:
        raise VaultError("条目的唯一编号无效。")


def _valid_title(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise VaultError("标题不能为空。")
    if len(value) > MAX_TITLE_LENGTH:
        raise VaultError(f"标题不能超过 {MAX_TITLE_LENGTH} 个字符。")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise VaultError("标题含有无法保存的字符。") from exc
    if "\x00" in value:
        raise VaultError("标题不能含有空字符。")


def _valid_body(value: Any) -> None:
    if not isinstance(value, str):
        raise VaultError("正文必须是纯文本。")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise VaultError("正文含有无法保存的字符。") from exc
    if size > MAX_BODY_BYTES:
        raise VaultError("单条正文不能超过 8 MiB。")


def _password_bytes(password: Any) -> bytes:
    """已有文件的密码只做旧版输入范围校验，不套用新密码设置规则。"""
    if not isinstance(password, str):
        raise VaultError("密码必须是文本。")
    if not LEGACY_MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise VaultError("密码长度必须为 8 至 1024 个字符。")
    try:
        return password.encode("utf-8")
    except UnicodeError as exc:
        raise VaultError("密码含有无法使用的字符。") from exc


def validate_new_password(password: Any) -> None:
    """轻量离线检查；仅用于设置密码，派生时始终使用用户原始文本。"""
    if not isinstance(password, str):
        raise VaultError("密码必须是文本。")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise VaultError("新密码长度必须为 16 至 1024 个字符，建议使用较长且不常见的口令。")
    _password_bytes(password)
    if not password.strip():
        raise VaultError("新密码不能全部为空白。")
    folded = password.casefold()
    # 此处只生成检查用文本；不裁剪、转换或截断实际参与派生的密码。
    compact = "".join(character for character in folded if character.isalnum())
    letters = "".join(character for character in compact if not character.isdecimal())
    common = compact in _COMMON_PASSWORD_WORDS or any(
        letters and letters == word * (len(letters) // len(word)) for word in _COMMON_PASSWORD_WORDS)
    repeated = any(candidate == (candidate[:size] * ((len(candidate) + size - 1) // size))[:len(candidate)]
                   for candidate in (folded, compact) if len(candidate) >= LEGACY_MIN_PASSWORD_LENGTH
                   for size in range(1, 5))
    sequential = len(compact) >= LEGACY_MIN_PASSWORD_LENGTH and any(
        compact in (sequence * ((len(compact) + len(sequence) - 1) // len(sequence) + 1))
        for sequence in ("0123456789", "9876543210", "abcdefghijklmnopqrstuvwxyz",
                         "zyxwvutsrqponmlkjihgfedcba", "qwertyuiopasdfghjklzxcvbnm"))
    if common or repeated or sequential:
        raise VaultError("新密码过于常见或规律，请使用更长且不常见的口令。")


def _validate_kdf(kdf: Any) -> None:
    _exact_keys(kdf, {"name", "salt", "n", "r", "p"})
    if (kdf["name"] != "scrypt" or type(kdf["n"]) is not int or type(kdf["r"]) is not int
            or type(kdf["p"]) is not int or (kdf["n"], kdf["r"], kdf["p"]) not in SUPPORTED_SCRYPT_PARAMETERS):
        raise VaultError("文件中的密码派生参数不受支持。")
    _decode(kdf["salt"], size=16)


def _valid_idle(value: Any) -> None:
    if type(value) is not int or not 0 <= value <= 120:
        raise VaultError("闲置锁定时间必须为 0 至 120 的整数，0 表示关闭。")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VaultError("文件格式错误：存在重复字段。")
        result[key] = value
    return result


def _validate_data(data: Any, expected_format: str) -> dict[str, Any]:
    fields = {"format", "version", "kdf", "check", "entries", "authentication"}
    if expected_format == VAULT_FORMAT:
        fields.add("idle_minutes")
    _exact_keys(data, fields)
    if data["format"] != expected_format or type(data["version"]) is not int or data["version"] != FORMAT_VERSION:
        raise VaultError("文件类型或版本不受支持，请使用本地密匣的对应文件。")
    if expected_format == VAULT_FORMAT:
        _valid_idle(data["idle_minutes"])
    kdf = data["kdf"]
    _validate_kdf(kdf)
    _exact_keys(data["check"], {"nonce", "ciphertext"})
    _decode(data["check"]["nonce"], size=12)
    _decode(data["check"]["ciphertext"], size=len(_CHECK_PLAIN) + 16)
    _exact_keys(data["authentication"], {"nonce", "ciphertext"})
    _decode(data["authentication"]["nonce"], size=12)
    _decode(data["authentication"]["ciphertext"], size=16)
    entries = data["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise VaultError(f"文件格式错误：条目数量不能超过 {MAX_ENTRIES}。")
    ids: set[str] = set()
    for record in entries:
        _exact_keys(record, {"id", "title", "nonce", "ciphertext"})
        _valid_id(record["id"])
        _valid_title(record["title"])
        if record["id"] in ids:
            raise VaultError("文件格式错误：条目的唯一编号重复。")
        ids.add(record["id"])
        _decode(record["nonce"], size=12)
        ciphertext = _decode(record["ciphertext"], maximum=MAX_BODY_BYTES + 16)
        if len(ciphertext) < 16:
            raise VaultError("文件格式错误：正文密文长度无效。")
    return data


def _parse(raw: bytes, expected_format: str) -> dict[str, Any]:
    if len(raw) > MAX_FILE_BYTES:
        raise VaultError("文件过大，最大允许 128 MiB。")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(VaultError("文件格式错误：无效数字。")))
        return _validate_data(data, expected_format)
    except VaultError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise VaultError("无法读取文件，文件格式无效或已损坏。") from exc


def _read_raw(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise VaultError(f"无法读取文件：{exc.strerror or '文件不可访问'}。") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise VaultError("文件过大，最大允许 128 MiB。")
    return raw


def _path(path: os.PathLike[str] | str) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, ValueError, TypeError) as exc:
        raise VaultError("文件路径无效。") from exc


def _fingerprint(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()


def _derive(password: str, kdf: dict[str, Any]) -> bytes:
    try:
        _validate_kdf(kdf)
        return Scrypt(salt=_decode(kdf["salt"], size=16), length=32,
                      n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(_password_bytes(password))
    except VaultError:
        raise
    except (ValueError, MemoryError) as exc:
        raise VaultError("无法完成密码派生，请确认内存充足后重试。") from exc


def _body_aad(entry_id: str, title: str) -> bytes:
    return _BODY_AAD + _canonical({"id": entry_id, "title": title})


def _encrypt_entry(entry: Entry, key: bytes) -> dict[str, str]:
    nonce = os.urandom(12)
    return {"id": entry.id, "title": entry.title, "nonce": _encode(nonce),
            "ciphertext": _encode(AESGCM(key).encrypt(nonce, entry.body.encode("utf-8"),
                                                      _body_aad(entry.id, entry.title)))}


def _decrypt_entry(record: dict[str, Any], key: bytes) -> Entry:
    try:
        plain = AESGCM(key).decrypt(_decode(record["nonce"], size=12),
                                   _decode(record["ciphertext"], maximum=MAX_BODY_BYTES + 16),
                                   _body_aad(record["id"], record["title"]))
        body = plain.decode("utf-8")
        _valid_body(body)
        return Entry(record["id"], record["title"], body)
    except (InvalidTag, UnicodeError) as exc:
        raise VaultError("正文无法读取，密码错误或文件已损坏。") from exc


def _authenticate(data: dict[str, Any], key: bytes) -> None:
    payload = {k: v for k, v in data.items() if k != "authentication"}
    nonce = os.urandom(12)
    data["authentication"] = {"nonce": _encode(nonce),
                              "ciphertext": _encode(AESGCM(key).encrypt(nonce, b"", _DIRECTORY_AAD + _canonical(payload)))}


def _verify(data: dict[str, Any], password: str) -> bytes:
    key = _derive(password, data["kdf"])
    try:
        check = data["check"]
        check_plain = AESGCM(key).decrypt(_decode(check["nonce"], size=12),
                                          _decode(check["ciphertext"]),
                                          _CHECK_AAD + data["format"].encode("ascii"))
        if check_plain != _CHECK_PLAIN:
            raise InvalidTag
        payload = {k: v for k, v in data.items() if k != "authentication"}
        auth = data["authentication"]
        AESGCM(key).decrypt(_decode(auth["nonce"], size=12), _decode(auth["ciphertext"], size=16),
                            _DIRECTORY_AAD + _canonical(payload))
        # 在解锁、导入及恢复时检查每条正文，绝不提交未经完整校验的文件。
        for record in data["entries"]:
            _decrypt_entry(record, key)
    except InvalidTag as exc:
        raise VaultError("密码错误，或文件已经损坏。") from exc
    return key


def _new_data(password: str, entries: list[Entry], file_format: str,
              idle_minutes: int = 5) -> tuple[dict[str, Any], bytes]:
    validate_new_password(password)
    kdf = {"name": "scrypt", "salt": _encode(os.urandom(16)), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}
    key = _derive(password, kdf)
    nonce = os.urandom(12)
    data: dict[str, Any] = {
        "format": file_format, "version": FORMAT_VERSION, "kdf": kdf,
        "check": {"nonce": _encode(nonce), "ciphertext": _encode(AESGCM(key).encrypt(
            nonce, _CHECK_PLAIN, _CHECK_AAD + file_format.encode("ascii")))},
        "entries": [_encrypt_entry(entry, key) for entry in entries],
    }
    if file_format == VAULT_FORMAT:
        data["idle_minutes"] = idle_minutes
    _authenticate(data, key)
    return data, key


def _serialize(data: dict[str, Any]) -> bytes:
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise VaultError("保险库过大，最大允许 128 MiB；请减少条目或正文长度。")
    return raw


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """系统负责释放崩溃进程的文件锁；锁文件不保存任何用户数据。"""
    lock_path = Path(str(path) + ".lock")
    stream = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b")
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\x00")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except VaultError:
        raise
    except OSError as exc:
        raise VaultError("文件正在被其他程序使用，或没有写入权限，请稍后重试。") from exc
    finally:
        if stream is not None:
            if acquired:
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()


def _existing_fingerprint(path: Path) -> bytes | None:
    try:
        if not path.exists():
            return None
        return _fingerprint(_read_raw(path))
    except OSError as exc:
        raise VaultError("无法检查目标文件。") from exc


def _temp_write(path: Path, raw: bytes) -> Path:
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise VaultError("无法写入临时文件，请检查磁盘空间和目录权限。") from exc


def _sync_directory(path: Path) -> None:
    # Windows 的 os.replace 使用同卷文件替换，目录句柄不支持普通 fsync。
    if os.name != "nt":
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass


def _atomic_write(path: Path, raw: bytes, expected: bytes | None) -> None:
    """只提交当前内容；失败保留原文件，不自动生成历史副本。"""
    temporary: Path | None = None
    with _file_lock(path):
        if _existing_fingerprint(path) != expected:
            raise VaultError("文件已被其他程序修改；请重新打开保险库后再操作，避免覆盖新数据。")
        try:
            temporary = _temp_write(path, raw)
            # 检测没有遵循本应用文件锁的其他写入者。
            if _existing_fingerprint(path) != expected:
                raise VaultError("文件已被其他程序修改；本次保存已取消。")
            os.replace(temporary, path)
            temporary = None
            _sync_directory(path.parent)
        except VaultError:
            raise
        except OSError as exc:
            raise VaultError("保存失败，原文件仍保留；请检查磁盘空间、文件占用和目录权限。") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _serialized(method):
    """同一对象的调用依次提交，防止并发线程覆盖彼此的内存状态。"""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return invoke


class Vault:
    def __init__(self, path: os.PathLike[str] | str):
        self._mutex = threading.RLock()
        self.path = _path(path)
        self._recovery_only = False
        self._data: dict[str, Any] | None = None
        self._key: bytearray | None = None
        self._revision: bytes | None = None
        self._plans: dict[str, tuple[list[MergeItem], dict[str, int], bytes | None]] = {}
        if self.path.exists():
            raw = _read_raw(self.path)
            self._data = _parse(raw, VAULT_FORMAT)
            self._revision = _fingerprint(raw)

    @classmethod
    def for_restore(cls, path: os.PathLike[str] | str) -> Vault:
        """损坏文件的专用恢复入口；保留原字节指纹，禁止初始化覆盖。"""
        instance = cls.__new__(cls)
        instance._mutex = threading.RLock()
        instance.path = _path(path)
        instance._recovery_only = True
        instance._data = None
        instance._key = None
        instance._revision = _existing_fingerprint(instance.path)
        instance._plans = {}
        return instance

    @property
    def is_initialized(self) -> bool:
        return self._data is not None

    @property
    def is_unlocked(self) -> bool:
        return self._key is not None

    @property
    def idle_minutes(self) -> int:
        if self._recovery_only:
            raise VaultError("保险库只能从完整加密备份恢复，不能读取或初始化当前损坏文件。")
        return 5 if self._data is None else self._data["idle_minutes"]

    def _require_initialized(self) -> dict[str, Any]:
        if self._recovery_only:
            raise VaultError("保险库只能从完整加密备份恢复，不能读取或初始化当前损坏文件。")
        if self._data is None:
            raise VaultError("请先设置主密码，创建保险库。")
        return self._data

    def _require_key(self) -> bytes:
        self._require_initialized()
        if self._key is None:
            raise VaultError("保险库已锁定，请输入主密码解锁。")
        return bytes(self._key)

    def _assert_current(self) -> None:
        if _existing_fingerprint(self.path) != self._revision:
            raise VaultError("保险库文件已被其他程序修改，请重新打开后再操作。")

    def _install_key(self, key: bytes) -> None:
        if self._key is not None:
            self._key[:] = b"\x00" * len(self._key)
        self._key = bytearray(key)
        self._plans.clear()

    def _persist(self, data: dict[str, Any], key: bytes) -> None:
        _authenticate(data, key)
        raw = _serialize(data)
        _atomic_write(self.path, raw, self._revision)
        # 只有系统替换成功后才提交内存状态。
        self._data = data
        self._revision = _fingerprint(raw)
        self._plans.clear()

    def _entries(self, key: bytes) -> list[Entry]:
        return [_decrypt_entry(record, key) for record in self._require_initialized()["entries"]]

    @_serialized
    def list_titles(self) -> list[tuple[str, str]]:
        if self._recovery_only:
            self._require_initialized()
        if self._data is None:
            return []
        return [(record["id"], record["title"]) for record in self._data["entries"]]

    @_serialized
    def create(self, password: str) -> None:
        if self._recovery_only:
            raise VaultError("当前文件损坏，不能创建空保险库覆盖；请先从完整加密备份恢复。")
        if self.is_initialized:
            raise VaultError("保险库已经存在，不能重复创建。")
        data, key = _new_data(password, [], VAULT_FORMAT)
        self._persist(data, key)
        self._install_key(key)

    @_serialized
    def unlock(self, password: str) -> None:
        data = self._require_initialized()
        self._assert_current()
        key = _verify(data, password)
        self._install_key(key)

    @_serialized
    def lock(self) -> None:
        if self._key is not None:
            self._key[:] = b"\x00" * len(self._key)
        self._key = None
        self._plans.clear()

    @_serialized
    def get_entry(self, entry_id: str) -> Entry:
        key = self._require_key()
        self._assert_current()
        for record in self._require_initialized()["entries"]:
            if record["id"] == entry_id:
                return _decrypt_entry(record, key)
        raise VaultError("条目不存在，可能已经删除。")

    @_serialized
    def put_entry(self, title: str, body: str, entry_id: str | None = None) -> str:
        key = self._require_key()
        _valid_title(title)
        _valid_body(body)
        data = copy.deepcopy(self._require_initialized())
        records = data["entries"]
        if entry_id is None:
            if len(records) >= MAX_ENTRIES:
                raise VaultError(f"保险库最多允许 {MAX_ENTRIES} 个条目。")
            entry_id = str(uuid.uuid4())
            records.append(_encrypt_entry(Entry(entry_id, title, body), key))
        else:
            _valid_id(entry_id)
            for index, record in enumerate(records):
                if record["id"] == entry_id:
                    records[index] = _encrypt_entry(Entry(entry_id, title, body), key)
                    break
            else:
                raise VaultError("条目不存在，可能已经删除。")
        self._persist(data, key)
        return entry_id

    @_serialized
    def delete(self, entry_id: str) -> None:
        key = self._require_key()
        data = copy.deepcopy(self._require_initialized())
        records = data["entries"]
        remaining = [record for record in records if record["id"] != entry_id]
        if len(remaining) == len(records):
            raise VaultError("条目不存在，可能已经删除。")
        data["entries"] = remaining
        self._persist(data, key)

    @_serialized
    def change_password(self, new_password: str) -> None:
        old_key = self._require_key()
        self._assert_current()
        data, new_key = _new_data(new_password, self._entries(old_key), VAULT_FORMAT, self.idle_minutes)
        self._persist(data, new_key)
        self._install_key(new_key)

    @_serialized
    def set_idle_minutes(self, minutes: int) -> None:
        key = self._require_key()
        _valid_idle(minutes)
        data = copy.deepcopy(self._require_initialized())
        data["idle_minutes"] = minutes
        self._persist(data, key)

    def _destination(self, path: os.PathLike[str] | str) -> Path:
        destination = _path(path)
        protected = (self.path, Path(str(self.path) + ".bak"), Path(str(self.path) + ".lock"))
        if any(os.path.normcase(str(destination)) == os.path.normcase(str(item)) for item in protected):
            raise VaultError("请另选文件名，不能覆盖当前保险库、旧版备份或锁文件。")
        return destination

    @_serialized
    def export_file(self, path: os.PathLike[str] | str, password: str,
                    ids: list[str] | tuple[str, ...] | set[str] | None = None) -> None:
        key = self._require_key()
        self._assert_current()
        destination = self._destination(path)
        selected: set[str] | None = None
        if ids is not None:
            if not isinstance(ids, (list, tuple, set)) or any(not isinstance(item, str) for item in ids):
                raise VaultError("导出的条目选择无效。")
            selected = set(ids)
            if not selected:
                raise VaultError("请至少勾选一个条目再导出。")
        entries = self._entries(key)
        if selected is not None:
            all_ids = {entry.id for entry in entries}
            if not selected <= all_ids:
                raise VaultError("选择中含有已经删除或不存在的条目。")
            entries = [entry for entry in entries if entry.id in selected]
        data, _ = _new_data(password, entries, EXPORT_FORMAT)
        raw = _serialize(data)
        expected = _existing_fingerprint(destination)
        _atomic_write(destination, raw, expected)

    @_serialized
    def inspect_import(self, path: os.PathLike[str] | str, password: str) -> ImportPlan:
        key = self._require_key()
        self._assert_current()
        incoming_data = _parse(_read_raw(_path(path)), EXPORT_FORMAT)
        incoming_key = _verify(incoming_data, password)
        incoming = [_decrypt_entry(record, incoming_key) for record in incoming_data["entries"]]
        local_entries = self._entries(key)
        by_id = {entry.id: entry for entry in local_entries}
        by_content: dict[tuple[str, str], Entry] = {}
        by_title: dict[str, Entry] = {}
        for entry in local_entries:
            by_content.setdefault((entry.title, entry.body), entry)
            by_title.setdefault(entry.title, entry)
        local_content = set(by_content)
        items: list[MergeItem] = []
        counts = {kind: 0 for kind in ("new", "duplicate", "conflict", "possible_duplicate")}
        for entry in incoming:
            exact = by_content.get((entry.title, entry.body))
            # 本地已有同文仍可自动去重；导入前项的同文不能遮住编号冲突。
            if (entry.title, entry.body) in local_content:
                item = MergeItem(entry, "duplicate", exact)
            elif entry.id in by_id:
                item = MergeItem(entry, "conflict", by_id[entry.id])
            elif exact is not None:
                item = MergeItem(entry, "duplicate", exact)
            elif entry.title in by_title:
                item = MergeItem(entry, "possible_duplicate", by_title[entry.title])
            else:
                item = MergeItem(entry, "new")
            items.append(item)
            counts[item.kind] += 1
            # 此处仅作预览分类；最终去重必须结合全部冲突选择。
            by_content.setdefault((entry.title, entry.body), entry)
            by_title.setdefault(entry.title, entry)
        token = str(uuid.uuid4())
        self._plans.clear()
        self._plans[token] = (list(items), dict(counts), self._revision)
        return ImportPlan(list(items), dict(counts), token)

    @_serialized
    def commit_import(self, plan: ImportPlan, choices: dict[str, str]) -> None:
        key = self._require_key()
        if not isinstance(plan, ImportPlan) or plan._token not in self._plans:
            raise VaultError("导入预览已经过期，请重新选择文件并预览。")
        items, counts, revision = self._plans[plan._token]
        if plan.items != items or plan.counts != counts or revision != self._revision:
            raise VaultError("导入预览已被修改或已经过期，请重新预览。")
        self._assert_current()
        if not isinstance(choices, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in choices.items()):
            raise VaultError("冲突处理选择无效。")
        selectable = {item.incoming.id for item in items if item.kind in ("conflict", "possible_duplicate")}
        if set(choices) - selectable:
            raise VaultError("冲突处理选择中含有不需要处理的条目。")
        for item in items:
            choice = choices.get(item.incoming.id)
            if item.kind == "conflict" and choice not in ("local", "import", "both"):
                raise VaultError("请为每个编号冲突选择保留本地、采用导入内容或两份都保留。")
            if item.kind == "possible_duplicate" and choice is not None and choice not in ("local", "both"):
                raise VaultError("可能重复的条目只能选择保留本地或两份都保留。")
        entries = self._entries(key)
        replacements = {item.incoming.id: item.incoming for item in items
                        if item.kind == "conflict" and choices[item.incoming.id] == "import"}
        # 先移除全部明确覆盖的旧版本，避免后续覆盖移走前面使用的去重依据。
        # 不整理无关本地条目，包括本地原先就存在的同文条目。
        result = {entry.id: entry for entry in entries if entry.id not in replacements}
        content = {(entry.title, entry.body) for entry in result.values()}
        for local in entries:
            entry = replacements.get(local.id)
            if entry is not None and (entry.title, entry.body) not in content:
                result[entry.id] = entry
                content.add((entry.title, entry.body))
        # 覆盖结果已确定，所有接受的追加候选（含预览重复项）再按完整内容去重。
        for item in sorted(items, key=lambda item: item.incoming.id):
            entry = item.incoming
            choice = choices.get(entry.id, "both" if item.kind == "possible_duplicate" else "import")
            if choice == "local" or entry.id in replacements:
                continue
            if (entry.title, entry.body) in content:
                continue
            if choice == "both" or entry.id in result:
                entry = Entry(str(uuid.uuid4()), entry.title, entry.body)
            result[entry.id] = entry
            content.add((entry.title, entry.body))
        if len(result) > MAX_ENTRIES:
            raise VaultError(f"合并后条目数量超过 {MAX_ENTRIES}，导入已取消。")
        if list(result.values()) == entries:
            # 全部重复或保留本地时无需写盘，但已使用的计划不能再次提交。
            self._plans.clear()
            return
        data = copy.deepcopy(self._require_initialized())
        data["entries"] = [_encrypt_entry(entry, key) for entry in result.values()]
        self._persist(data, key)

    @_serialized
    def backup_file(self, path: os.PathLike[str] | str) -> None:
        self._require_initialized()
        self._assert_current()
        destination = self._destination(path)
        raw = _read_raw(self.path)
        if _fingerprint(raw) != self._revision:
            raise VaultError("保险库文件已被修改，备份已取消。")
        expected = _existing_fingerprint(destination)
        _atomic_write(destination, raw, expected)

    @_serialized
    def restore_backup(self, path: os.PathLike[str] | str, password: str) -> None:
        source = _path(path)
        raw = _read_raw(source)
        data = _parse(raw, VAULT_FORMAT)
        key = _verify(data, password)
        # 不写入来源路径，也不生成恢复前副本；来源别名或硬链接同样得以保留。
        _atomic_write(self.path, raw, self._revision)
        self._data = data
        self._revision = _fingerprint(raw)
        self._install_key(key)
        self._recovery_only = False

    @_serialized
    def seal_draft(self, entry: Entry) -> bytes:
        key = self._require_key()
        if not isinstance(entry, Entry):
            raise VaultError("未保存的编辑内容无效。")
        _valid_id(entry.id, allow_empty=True)
        # 草稿允许暂时不符合普通保存限制的编辑，避免超长标题或正文阻止锁定。
        if not isinstance(entry.title, str) or not isinstance(entry.body, str):
            raise VaultError("草稿标题和正文必须是文本。")
        try:
            plain = _canonical({"id": entry.id, "title": entry.title, "body": entry.body})
        except UnicodeError as exc:
            raise VaultError("草稿含有无法保存的字符。") from exc
        if len(plain) + 28 > MAX_FILE_BYTES:
            raise VaultError("未保存的编辑过大，内存草稿上限为 128 MiB；请先缩短正文。")
        nonce = os.urandom(12)
        aad = _DRAFT_AAD + _decode(self._require_initialized()["kdf"]["salt"], size=16)
        return nonce + AESGCM(key).encrypt(nonce, plain, aad)

    @_serialized
    def open_draft(self, blob: bytes) -> Entry:
        key = self._require_key()
        if not isinstance(blob, bytes) or not 28 <= len(blob) <= MAX_FILE_BYTES:
            raise VaultError("未保存的编辑内容已经损坏。")
        aad = _DRAFT_AAD + _decode(self._require_initialized()["kdf"]["salt"], size=16)
        try:
            plain = AESGCM(key).decrypt(blob[:12], blob[12:], aad)
            value = json.loads(plain.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
            _exact_keys(value, {"id", "title", "body"})
            _valid_id(value["id"], allow_empty=True)
            if not isinstance(value["title"], str) or not isinstance(value["body"], str):
                raise VaultError("草稿标题和正文必须是文本。")
            value["title"].encode("utf-8")
            value["body"].encode("utf-8")
            return Entry(value["id"], value["title"], value["body"])
        except (InvalidTag, ValueError, UnicodeError, RecursionError) as exc:
            raise VaultError("未保存的编辑内容无法恢复，密码已变更或草稿已经损坏。") from exc
