"""数据版本指纹：用 DuckDB 主文件 + WAL 的 (mtime, size) 生成短版本号。

为什么不用内容哈希：数据库有几百 MB 时每次哈希太贵；而 mtime+size 对
"有没有写过"这个判断已经足够，且能从 WAL 捕捉到尚未 checkpoint 的写入。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataSignature:
    version: str
    size: int


def data_signature(db_path: Path) -> DataSignature:
    parts: list[str] = []
    size = 0
    for path in (Path(db_path), Path(f"{db_path}.wal")):
        try:
            st = path.stat()
        except FileNotFoundError:
            parts.append(f"{path.name}:missing")
            continue
        parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
        size += st.st_size
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]
    return DataSignature(version=f"v{digest}", size=size)
