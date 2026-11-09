"""把 aicg3d-PoseSwap 的姿势 / 服装图库转成 LocalGen 可用的格式。

源目录结构（只读，不改动）：
    <SRC>/library/{img,thumbs}/...     姿势图
    <SRC>/library/data/pose-zh.json    姿势索引（1439 条，中文键）
    <SRC>/garments/{img}/...           服装图
    <SRC>/garments/data/garment.json   服装索引

输出：
    <DST>/poses/index.json    统一 id 主键 + 英文键
    <DST>/poses/img/...       原图（webp 原样保留，浏览器能显示）
    <DST>/poses/thumbs/...    缩略图
    <DST>/poses/LICENCE.md    许可声明（原样拷贝）
    <DST>/garments/index.json
    <DST>/garments/img/...
    <DST>/garments/LICENCE.md

为什么不改图片格式：webp 在 Qt 6 里原生可读（QImageReader 支持），
转 png 会让 380 MB 变 1 GB 以上，没有收益。

用法:
    python tools/import_pose_libraries.py --src "D:\\Comfy-Desktop\\..." --dst data/libraries
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# 源索引的中文键 → 目标英文键。只映射我们要用的，其余原样保留在 raw 里。
POSE_KEYS = {
    "id": "id",
    "体系": "collection",
    "姿势编号": "code",
    "分类": "category",
    "姿势名称": "title",
    "角度": "angle",
    "缩略图": "thumb",
    "原图": "image",
    "检索": "search",
}


def norm_pose(item: dict) -> dict:
    out = {POSE_KEYS.get(k, k): v for k, v in item.items()}
    out["kind"] = "pose"
    return out


def norm_garment(item: dict, index: int) -> dict:
    """服装索引已是英文键：id / cat / cat_cn / desc / img。

    desc 是英文服装描述（形如 "ivory frill knit denim mini skirt coat socks
    white sneakers"），原插件就是把它追加进换装提示词的——鞋袜这类小件只有
    明确说出来才会被替换。这里原样保留，字段名统一成 LocalGen 的用词。
    """
    out = {
        "id": item.get("id") or f"garment_{index:04d}",
        "kind": "garment",
        "category": item.get("cat") or "",
        "category_cn": item.get("cat_cn") or "",
        "description": item.get("desc") or "",
        "image": item.get("img") or "",
    }
    return out


def copy_tree(src: Path, dst: Path, label: str) -> int:
    if not src.is_dir():
        print(f"  [{label}] 源目录不存在，跳过: {src}")
        return 0
    n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for file in src.rglob("*"):
        if not file.is_file():
            continue
        target = dst / file.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != file.stat().st_size:
            shutil.copy2(file, target)
        n += 1
    print(f"  [{label}] 复制 {n} 个文件 -> {dst}")
    return n


def import_poses(src: Path, dst: Path) -> None:
    print("姿势库：")
    root = dst / "poses"
    index_src = src / "library" / "data" / "pose-zh.json"
    if not index_src.is_file():
        print(f"  索引缺失: {index_src}")
        return
    raw = json.loads(index_src.read_text("utf-8"))
    items = [norm_pose(it) for it in raw]
    # 按 code 聚合角度，便于「先选姿势再看多角度」
    groups: dict[str, list[str]] = {}
    for it in items:
        groups.setdefault(it.get("code", ""), []).append(it["id"])
    payload = {
        "version": 1,
        "kind": "pose",
        "count": len(items),
        "groups": groups,
        "collections": list(dict.fromkeys(it.get("collection", "") for it in items
                                          if it.get("collection"))),
        "categories": list(dict.fromkeys(it.get("category", "") for it in items
                                         if it.get("category"))),
        "items": items,
    }
    copy_tree(src / "library" / "img", root / "img", "poses/img")
    copy_tree(src / "library" / "thumbs", root / "thumbs", "poses/thumbs")
    for name in ("LICENSE", "ASSET_NOTICE.md", "SOURCE.json", "README.md"):
        f = src / "library" / name
        if f.is_file():
            shutil.copy2(f, root / f"LICENCE-{name}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    print(f"  索引 {len(items)} 条 / {len(groups)} 个姿势编号 -> {root / 'index.json'}")


def import_garments(src: Path, dst: Path) -> None:
    print("服装库：")
    root = dst / "garments"
    index_src = src / "garments" / "data" / "garment.json"
    if not index_src.is_file():
        print(f"  索引缺失: {index_src}")
        return
    raw = json.loads(index_src.read_text("utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw
    items = [norm_garment(it, i) for i, it in enumerate(records) if isinstance(it, dict)]
    copy_tree(src / "garments" / "img", root / "img", "garments/img")
    for name in ("NOTICE.md", "SOURCE.json", "LICENSE"):
        f = src / "garments" / name
        if f.is_file():
            shutil.copy2(f, root / f"LICENCE-{name}")
    root.mkdir(parents=True, exist_ok=True)
    categories = raw.get("cats_cn", []) if isinstance(raw, dict) else []
    payload = {"version": 1, "kind": "garment", "count": len(items),
               "categories": categories, "items": items}
    (root / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    print(f"  索引 {len(items)} 条 / {len(categories)} 个分类 -> {root / 'index.json'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dst", required=True, type=Path)
    parser.add_argument("--only", choices=["poses", "garments", "all"], default="all")
    args = parser.parse_args()

    src = args.src.resolve()
    dst = args.dst.resolve()
    if not (src / "library").is_dir() and not (src / "garments").is_dir():
        print(f"源目录不像 aicg3d-PoseSwap: {src}")
        return 2
    print(f"源: {src}\n目标: {dst}\n")
    if args.only in ("poses", "all"):
        import_poses(src, dst)
    if args.only in ("garments", "all"):
        import_garments(src, dst)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
