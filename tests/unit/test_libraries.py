"""图库的读取、检索、出图与路径越权防护。"""
import json

import pytest

from localgen.jobs.models import AppError
from localgen.storage.libraries import LibraryStore

POSES = [
    {"id": "P001_front", "code": "P001", "title": "S形站姿", "category": "站姿",
     "angle": "正面", "collection": "全身单人", "image": "img/P001_front.webp",
     "thumb": "thumbs/P001_front.jpg", "search": "站姿 正面"},
    {"id": "P001_high", "code": "P001", "title": "S形站姿", "category": "站姿",
     "angle": "俯视", "collection": "全身单人", "image": "img/P001_high.webp",
     "thumb": "thumbs/P001_high.jpg", "search": "站姿 俯视"},
    {"id": "P002_front", "code": "P002", "title": "坐姿", "category": "坐姿",
     "angle": "正面", "collection": "上半身", "image": "img/P002_front.webp",
     "thumb": "thumbs/P002_front.jpg", "search": "坐姿"},
]
GARMENTS = [
    {"id": "SPR-0001", "category": "春", "category_cn": "春装",
     "description": "ivory frill knit denim mini skirt", "image": "img/SPR-0001.webp"},
    {"id": "SWM-0001", "category": "水着", "category_cn": "泳装",
     "description": "mint floral pants sandals", "image": "img/SWM-0001.webp"},
]


def make_library(tmp_path):
    root = tmp_path / "libraries"
    for kind, payload, images in (
        ("poses", {"version": 1, "kind": "pose", "count": len(POSES),
                   "groups": {"P001": ["P001_front", "P001_high"], "P002": ["P002_front"]},
                   "collections": ["全身单人", "上半身"],
                   "categories": ["站姿", "坐姿"], "items": POSES},
         ["img/P001_front.webp", "img/P001_high.webp", "img/P002_front.webp",
          "thumbs/P001_front.jpg", "thumbs/P001_high.jpg", "thumbs/P002_front.jpg"]),
        ("garments", {"version": 1, "kind": "garment", "count": len(GARMENTS),
                      "categories": ["春装", "泳装"], "items": GARMENTS},
         ["img/SPR-0001.webp", "img/SWM-0001.webp"]),
    ):
        folder = root / kind
        for relative in images:
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-" + relative.encode())
        (folder / "index.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return root


def test_installed_and_counts(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    assert store.installed("poses") and store.installed("garments")
    assert store.index("poses")["count"] == 3
    assert store.index("garments")["count"] == 2


def test_unknown_kind_rejected(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    with pytest.raises(AppError) as caught:
        store.index("stickers")
    assert caught.value.status == 404


def test_not_installed(tmp_path):
    store = LibraryStore(tmp_path, tmp_path / "nope")
    assert store.installed("poses") is False
    with pytest.raises(AppError) as caught:
        store.index("poses")
    assert caught.value.detail.code == "LIBRARY_NOT_INSTALLED"


def test_catalog_paginates(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    page = store.catalog("poses", limit=2)
    assert page["total"] == 3 and len(page["items"]) == 2
    page2 = store.catalog("poses", limit=2, offset=2)
    assert len(page2["items"]) == 1


def test_catalog_filters(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    assert store.catalog("poses", query="坐姿")["total"] == 1
    assert store.catalog("poses", category="站姿")["total"] == 2
    assert store.catalog("poses", collection="上半身")["total"] == 1
    # 服装按中文分类过滤（cat_cn）
    assert store.catalog("garments", category="泳装")["total"] == 1


def test_catalog_lists_facets(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    poses = store.catalog("poses")
    assert poses["collections"] == ["全身单人", "上半身"]
    assert "站姿" in poses["categories"]
    garments = store.catalog("garments")
    assert garments["categories"] == ["春装", "泳装"]


def test_entry_and_missing(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    assert store.entry("poses", "P001_front")["angle"] == "正面"
    with pytest.raises(AppError) as caught:
        store.entry("poses", "nope")
    assert caught.value.detail.code == "LIBRARY_ITEM_NOT_FOUND"


def test_image_and_thumb(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    path, media = store.image("poses", "P001_front")
    assert path.name == "P001_front.webp" and media == "image/webp"
    thumb, tmedia = store.thumb("poses", "P001_front")
    assert thumb.name == "P001_front.jpg" and tmedia == "image/jpeg"
    # 服装库没有 thumb 字段 -> 回退原图
    fallback, _ = store.thumb("garments", "SPR-0001")
    assert fallback.name == "SPR-0001.webp"


def test_thumb_falls_back_when_file_missing(tmp_path):
    root = make_library(tmp_path)
    (root / "poses" / "thumbs" / "P001_front.jpg").unlink()
    store = LibraryStore(tmp_path, root)
    thumb, _ = store.thumb("poses", "P001_front")
    assert thumb.name == "P001_front.webp"


def test_path_traversal_rejected(tmp_path):
    root = make_library(tmp_path)
    payload = json.loads((root / "poses" / "index.json").read_text("utf-8"))
    payload["items"][0]["image"] = "../../../etc/passwd"
    (root / "poses" / "index.json").write_text(json.dumps(payload), "utf-8")
    store = LibraryStore(tmp_path, root)
    with pytest.raises(AppError) as caught:
        store.image("poses", "P001_front")
    assert caught.value.detail.code == "INVALID_LIBRARY_PATH"


def test_source_path_for_engine(tmp_path):
    store = LibraryStore(tmp_path, make_library(tmp_path))
    assert store.source_path("poses", "P001_front").is_file()


def test_reload_drops_cache(tmp_path):
    root = make_library(tmp_path)
    store = LibraryStore(tmp_path, root)
    assert store.index("poses")["count"] == 3
    payload = json.loads((root / "poses" / "index.json").read_text("utf-8"))
    payload["count"] = 99
    payload["items"] = payload["items"][:1]
    (root / "poses" / "index.json").write_text(json.dumps(payload), "utf-8")
    assert store.index("poses")["count"] == 3          # 仍是缓存
    store.reload()
    assert store.index("poses")["count"] == 99
