"""姿势 / 服装图库：索引加载、检索、出图。

图库是只读数据，放在 data/libraries/<kind>/ 下，由 tools/import_pose_libraries.py
导入。这里只做读取，不做写入 —— 素材是第三方的，误改会破坏溯源性。

对外提供的能力：
- catalog(kind, query, category, collection, limit, offset) -> 分页条目
- image(kind, item_id) -> (Path, media_type)   原图
- thumb(kind, item_id) -> (Path, media_type)   缩略图（服装库没有，回退原图）
- entry(kind, item_id) -> dict                 单条完整记录
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from localgen.config import guarded
from localgen.jobs.models import AppError

KINDS = ("poses", "garments")
MEDIA = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".png": "image/png"}

# 检索时要匹配的文本字段，按 kind 不同。
SEARCH_FIELDS = {
    "poses": ("id", "code", "title", "collection", "category", "angle", "search"),
    "garments": ("id", "category", "category_cn", "description"),
}


class LibraryStore:
    """只读图库。索引在首次访问时加载并缓存，进程内共享。"""

    def __init__(self, data: Path, root: Path | None = None):
        self.root = guarded(root or (Path(data) / "libraries"))
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------- 索引 ----------

    def index(self, kind: str) -> dict:
        if kind not in KINDS:
            raise AppError(404, "LIBRARY_NOT_FOUND", "未知的图库类型")
        with self._lock:
            cached = self._cache.get(kind)
            if cached is not None:
                return cached
            path = guarded(self.root / kind / "index.json")
            if not path.is_file():
                raise AppError(404, "LIBRARY_NOT_INSTALLED", "图库尚未导入")
            payload = json.loads(path.read_text("utf-8-sig"))
            by_id = {item["id"]: item for item in payload.get("items", []) if item.get("id")}
            payload["by_id"] = by_id
            self._cache[kind] = payload
            return payload

    def installed(self, kind: str) -> bool:
        try:
            self.index(kind)
            return True
        except (AppError, OSError, ValueError):
            return False

    def reload(self) -> None:
        """重新导入图库后调用，丢掉缓存。"""
        with self._lock:
            self._cache.clear()

    # ---------- 检索 ----------

    def catalog(self, kind: str, *, query: str = "", category: str = "",
                collection: str = "", limit: int = 60, offset: int = 0) -> dict:
        payload = self.index(kind)
        items = payload.get("items", [])
        needle = query.strip().lower()
        if needle:
            fields = SEARCH_FIELDS.get(kind, ("id",))
            items = [it for it in items
                     if any(needle in str(it.get(f, "")).lower() for f in fields)]
        if category:
            items = [it for it in items
                     if category in (it.get("category"), it.get("category_cn"))]
        if collection and kind == "poses":
            items = [it for it in items if it.get("collection") == collection]
        total = len(items)
        page = items[offset:offset + limit]
        return {
            "kind": kind,
            "total": total,
            "offset": offset,
            "limit": limit,
            "categories": self._categories(payload, kind),
            "collections": payload.get("collections", []),
            "items": page,
        }

    @staticmethod
    def _categories(payload: dict, kind: str) -> list[str]:
        if kind == "garments":
            return list(payload.get("categories") or [])
        # 姿势库：从条目里聚出「分类」（站姿/跪姿/…），保持出现顺序。
        seen: list[str] = []
        for item in payload.get("items", []):
            value = item.get("category")
            if value and value not in seen:
                seen.append(value)
        return seen

    def entry(self, kind: str, item_id: str) -> dict:
        payload = self.index(kind)
        item = payload["by_id"].get(item_id)
        if item is None:
            raise AppError(404, "LIBRARY_ITEM_NOT_FOUND", "图库中找不到该条目")
        return item

    # ---------- 出图 ----------

    def _resolve(self, kind: str, relative: str) -> Path:
        """把索引里的相对路径解析成绝对路径，并拒绝越权路径。"""
        candidate = (self.root / kind / relative).resolve()
        base = (self.root / kind).resolve()
        if base not in candidate.parents and candidate != base:
            raise AppError(400, "INVALID_LIBRARY_PATH", "图库路径越界")
        if not candidate.is_file():
            raise AppError(404, "LIBRARY_ASSET_MISSING", "图库文件缺失")
        return candidate

    def image(self, kind: str, item_id: str) -> tuple[Path, str]:
        item = self.entry(kind, item_id)
        relative = item.get("image") or ""
        if not relative:
            raise AppError(404, "LIBRARY_ASSET_MISSING", "该条目没有原图")
        path = self._resolve(kind, relative)
        return path, MEDIA.get(path.suffix.lower(), "application/octet-stream")

    def thumb(self, kind: str, item_id: str) -> tuple[Path, str]:
        item = self.entry(kind, item_id)
        relative = item.get("thumb") or item.get("image") or ""
        if not relative:
            raise AppError(404, "LIBRARY_ASSET_MISSING", "该条目没有缩略图")
        try:
            path = self._resolve(kind, relative)
        except AppError:
            # 缩略图缺失时回退原图，UI 不该因此空白。
            return self.image(kind, item_id)
        return path, MEDIA.get(path.suffix.lower(), "application/octet-stream")

    def source_path(self, kind: str, item_id: str) -> Path:
        """给引擎用：拿到原图的绝对路径，用于拼 --ref-image。"""
        return self.image(kind, item_id)[0]
