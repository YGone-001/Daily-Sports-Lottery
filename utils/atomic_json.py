"""
原子 JSON 持久化
================
为运行时数据文件提供「不会写出半截 JSON」的落盘能力。

写入流程：
    同目录临时文件 -> json.dump -> flush -> fsync -> os.replace

`os.replace` 在同一文件系统内是原子操作，因此目标文件要么是旧的完整内容，
要么是新的完整内容，不会出现并发读到破损 JSON 的情况。

序列化统一使用 `sort_keys=True`，保证相同内容产生相同字节，便于校验与去重。
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def atomic_write_json(path: str, data: Any) -> None:
    """
    以原子方式把 `data` 序列化写入 `path`。

    - 在目标文件同目录创建临时文件，避免跨设备 rename。
    - flush + fsync 后再 os.replace，确保数据已落盘。
    - 失败时清理临时文件，不触碰原文件。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_json_file(path: str, default: Any) -> Any:
    """
    读取 JSON 文件。

    - 文件不存在 -> 返回 `default`（调用方据此惰性初始化）。
    - 内容损坏 -> 把原文件重命名为 `<path>.corrupt` 保留现场，返回 `default`。
      这是有意为之的降级行为：原子写入已能避免正常路径下的损坏，此处仅防御
      外部篡改或磁盘异常，避免整个应用因单个文件不可读而无法启动。
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[atomic_json] 无法读取 {path}: {exc}；已隔离为 .corrupt 并使用默认值")
        try:
            os.replace(path, f"{path}.corrupt")
        except OSError:
            pass
        return default
