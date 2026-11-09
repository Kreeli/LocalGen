from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from localgen.imaging import (
    clamp_fractional, cover_box, fractional_box, normalize_reference, pixel_box, reference_png,
)


def stripes(size=(600, 300)):
    image = Image.new("RGB", size, (200, 20, 20))
    draw = ImageDraw.Draw(image)
    third = size[0] // 3
    draw.rectangle((third, 0, 2 * third - 1, size[1] - 1), fill=(20, 200, 20))
    draw.rectangle((2 * third, 0, size[0] - 1, size[1] - 1), fill=(20, 20, 200))
    return image


def png(image):
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_cover_box_keeps_target_ratio_and_centres():
    assert cover_box((600, 300), (768, 768)) == (150, 0, 450, 300)
    assert cover_box((300, 600), (768, 768)) == (0, 150, 300, 450)
    assert cover_box((1024, 768), (768, 1024)) == (224, 0, 800, 768)
    # 已经是目标比例时不裁
    assert cover_box((512, 512), (1024, 1024)) == (0, 0, 512, 512)


def test_cover_box_rejects_degenerate_sizes():
    with pytest.raises(ValueError):
        cover_box((0, 100), (512, 512))
    with pytest.raises(ValueError):
        cover_box((100, 100), (0, 512))


def test_fractional_and_pixel_boxes_round_trip():
    assert pixel_box((0.25, 0.0, 0.75, 1.0), (400, 200)) == (100, 0, 300, 200)
    assert fractional_box((100, 0, 300, 200), (400, 200)) == (0.25, 0.0, 0.75, 1.0)
    # 反序输入、越界输入都被夹紧
    assert pixel_box((0.75, 1.0, 0.25, 0.0), (400, 200)) == (100, 0, 300, 200)
    assert pixel_box((-1, -1, 2, 2), (400, 200)) == (0, 0, 400, 200)
    with pytest.raises(ValueError):
        clamp_fractional((0.1, 0.2, 0.3))


def test_normalize_reference_crops_instead_of_stretching():
    """600×300 的三段竖条压成 1:1：只允许裁掉两侧，比例不能被压扁。"""
    result = normalize_reference(stripes(), (256, 256))
    assert result.size == (256, 256)
    assert result.getpixel((6, 128))[0] > 120        # 左缘仍是红
    assert result.getpixel((66, 128))[1] > 120       # 拉伸时这里会是红
    assert result.getpixel((250, 128))[2] > 120      # 右缘仍是蓝


def test_reference_png_honours_custom_crop_box():
    content = png(stripes())
    left = reference_png(content, (128, 128), (0.0, 0.0, 0.25, 0.5))
    with Image.open(BytesIO(left)) as image:
        assert image.size == (128, 128)
        assert image.getpixel((64, 64))[0] > 120 and image.getpixel((64, 64))[1] < 80
    right = reference_png(content, (128, 128), (0.75, 0.5, 1.0, 1.0))
    with Image.open(BytesIO(right)) as image:
        assert image.getpixel((64, 64))[2] > 120


def test_reference_png_applies_exif_orientation():
    image = Image.new("RGB", (40, 20), (10, 120, 200))
    exif = image.getexif()
    exif[274] = 6                     # 顺时针 90°
    buffer = BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    assert reference_png(buffer.getvalue(), (32, 32)).startswith(b"\x89PNG")
    from localgen.imaging import open_rgb

    assert open_rgb(buffer.getvalue()).size == (20, 40)
