"""参考图几何处理：按目标比例裁切，绝不拉伸。

引擎与桌面共用这一份实现，因此"用户在裁切对话框里框了什么"和
"引擎真正喂给模型的像素"永远是同一套算法：

1. 先按 EXIF 摆正方向、转成 RGB；
2. 按目标输出比例取裁切框（缺省 = 居中最大裁切，也接受用户给的归一化框）；
3. 最后缩放到目标尺寸。因为裁切框比例等于目标比例，这一步是等比缩放，
   不会把脸压扁、也不会把圆压成椭圆。
"""
from __future__ import annotations

import io
from typing import Iterable

from PIL import Image, ImageOps

Box = tuple[int, int, int, int]
Size = tuple[int, int]
FractionalBox = tuple[float, float, float, float]
SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def open_rgb(content: bytes) -> Image.Image:
    """解码图片字节，按 EXIF 摆正并转成 RGB（动图按首帧处理）。"""
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        try:
            oriented = ImageOps.exif_transpose(image)
        except Exception:  # 损坏的 EXIF 不应让整张图不可用
            oriented = image
        return oriented.convert("RGB")


def cover_box(size: Size, target: Size) -> Box:
    """`size` 里与 `target` 同比例的最大居中框（像素坐标，右/下开区间）。"""
    width, height = int(size[0]), int(size[1])
    target_w, target_h = int(target[0]), int(target[1])
    if min(width, height, target_w, target_h) <= 0:
        raise ValueError("尺寸必须为正数")
    if width * target_h >= height * target_w:
        # 源图比目标更宽：裁掉左右
        crop_w, crop_h = max(1, round(height * target_w / target_h)), height
    else:
        # 源图比目标更高：裁掉上下
        crop_w, crop_h = width, max(1, round(width * target_h / target_w))
    crop_w, crop_h = min(crop_w, width), min(crop_h, height)
    left, top = (width - crop_w) // 2, (height - crop_h) // 2
    return left, top, left + crop_w, top + crop_h


def clamp_fractional(box: Iterable[float]) -> FractionalBox:
    """把归一化裁切框夹进 0–1，并保证宽高都不为零。"""
    values = []
    for value in box:
        try:
            values.append(min(1.0, max(0.0, float(value))))
        except (TypeError, ValueError):
            raise ValueError("裁切框必须是 0–1 之间的数值") from None
    if len(values) != 4:
        raise ValueError("裁切框必须是四个数值")
    x0, y0, x1, y1 = values
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    minimum = 1e-4
    if right - left < minimum:
        right = min(1.0, left + minimum)
        left = max(0.0, right - minimum)
    if bottom - top < minimum:
        bottom = min(1.0, top + minimum)
        top = max(0.0, bottom - minimum)
    return left, top, right, bottom


def pixel_box(box: Iterable[float], size: Size) -> Box:
    """归一化裁切框 → 像素框（至少 1×1，且完全落在图内）。"""
    width, height = int(size[0]), int(size[1])
    left, top, right, bottom = clamp_fractional(box)
    x0 = max(0, min(width - 1, round(left * width)))
    y0 = max(0, min(height - 1, round(top * height)))
    x1 = max(x0 + 1, min(width, round(right * width)))
    y1 = max(y0 + 1, min(height, round(bottom * height)))
    return x0, y0, x1, y1


def fractional_box(box: Box, size: Size) -> FractionalBox:
    """像素框 → 归一化框（桌面裁切对话框回填用）。"""
    width, height = int(size[0]), int(size[1])
    x0, y0, x1, y1 = (int(value) for value in box)
    return (x0 / width, y0 / height, x1 / width, y1 / height)


def normalize_reference(image: Image.Image, target: Size, box: FractionalBox | None = None) -> Image.Image:
    """把参考图变成 `target` 尺寸的 RGB 图：先裁切到同比例，再等比缩放。"""
    target = (int(target[0]), int(target[1]))
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGB":
        image = image.convert("RGB")
    crop = cover_box(image.size, target) if box is None else pixel_box(box, image.size)
    image = image.crop(crop)
    if image.size != target:
        image = image.resize(target, Image.LANCZOS)
    return image


def reference_png(content: bytes, target: Size, box: FractionalBox | None = None) -> bytes:
    """上传/落盘用的参考图字节：始终是目标尺寸的 PNG。"""
    buffer = io.BytesIO()
    normalize_reference(open_rgb(content), target, box).save(buffer, "PNG")
    return buffer.getvalue()


def preview(image: Image.Image, limit: int = 1600) -> Image.Image:
    """裁切对话框显示用的缩略图（比例不变，只是省内存）。"""
    copy = image.copy()
    copy.thumbnail((int(limit), int(limit)), Image.LANCZOS)
    return copy
