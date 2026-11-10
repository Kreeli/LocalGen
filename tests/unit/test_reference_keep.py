"""ImageStore 的参考图 / 姿势图 / 服装图附属物，以及 reconcile 的豁免测试。"""
import json
from io import BytesIO

from PIL import Image

from localgen.config import Settings
from localgen.storage.images import (
    ATTACHMENTS, GARMENT_NAME, POSE_NAME, REFERENCE_NAME, ImageStore,
)

JOB = "0f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
ARTIFACT = "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"


def png(size=(768, 768)):
    buffer = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def store(tmp_path):
    return ImageStore(tmp_path, Settings(engine_mode="mock", min_free_mib=0))


def install_library(tmp_path, kind, item_id, size=(900, 1200)):
    """造一个最小可用的图库：index.json + 一张 webp 原图。"""
    folder = tmp_path / "libraries" / kind
    (folder / "img").mkdir(parents=True)
    buffer = BytesIO()
    Image.new("RGB", size, (200, 120, 90)).save(buffer, "WEBP")
    (folder / "img" / f"{item_id}.webp").write_bytes(buffer.getvalue())
    index = {"count": 1, "items": [{"id": item_id, "image": f"img/{item_id}.webp"}]}
    (folder / "index.json").write_text(json.dumps(index), "utf-8")
    return folder


def test_keep_reference_copies_the_engine_copy(tmp_path):
    images = store(tmp_path)
    source = tmp_path / "comfy-output" / JOB / "input.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(png())
    kept = images.keep_reference(JOB)
    assert kept == tmp_path / "results" / JOB / REFERENCE_NAME
    assert kept.read_bytes() == source.read_bytes()


def test_keep_reference_is_silent_when_engine_copy_is_gone(tmp_path):
    images = store(tmp_path)
    assert images.keep_reference(JOB) is None


def test_keep_reference_rejects_a_non_uuid_job(tmp_path):
    images = store(tmp_path)
    assert images.keep_reference("../../etc/passwd") is None


def test_keep_reference_finds_the_person_image_of_a_two_stage_job(tmp_path):
    """两段式 pose-swap 的引擎目录里没有 input.png，只有 ref0-person / ref1-pose。

    历史里"复用参数"要能拿回当初喂进去的人物图，所以这里必须认得出人物那张，
    而不是因为找不到 input.png 就静默放弃（那正是参考图变空的根因）。
    """
    images = store(tmp_path)
    folder = tmp_path / "comfy-output" / JOB
    folder.mkdir(parents=True)
    person = folder / "ref0-person.png"
    pose = folder / "ref1-pose.png"
    person.write_bytes(png())
    pose.write_bytes(png((512, 768)))
    kept = images.keep_reference(JOB, {"image": "abc.png", "pose_id": "P001"})
    assert kept == tmp_path / "results" / JOB / REFERENCE_NAME
    # 存下来的必须是人物图，不是姿势骨骼。
    assert kept.read_bytes() == person.read_bytes()


def test_keep_reference_ignores_a_lone_pose_skeleton(tmp_path):
    """只剩约束图、没有人物图时不该拿姿势骨骼冒充参考图。"""
    images = store(tmp_path)
    folder = tmp_path / "comfy-output" / JOB
    folder.mkdir(parents=True)
    (folder / "ref0-pose.png").write_bytes(png((512, 768)))
    (folder / "ref1-garment.png").write_bytes(png((512, 768)))
    assert images.keep_reference(JOB, {"image": "abc.png"}) is None


def test_keep_reference_falls_back_to_a_single_reference(tmp_path):
    """单参考任务换了命名也能存下来：只有一张 ref* 时它就是人物图。"""
    images = store(tmp_path)
    folder = tmp_path / "comfy-output" / JOB
    folder.mkdir(parents=True)
    only = folder / "ref0-source.png"
    only.write_bytes(png())
    kept = images.keep_reference(JOB, {"image": "abc.png"})
    assert kept is not None and kept.read_bytes() == only.read_bytes()


def test_keep_library_saves_pose_and_garment_as_png(tmp_path):
    install_library(tmp_path, "poses", "P001_front")
    install_library(tmp_path, "garments", "SPR-0001", size=(1366, 2048))
    images = store(tmp_path)
    kept = images.keep_library(JOB, {"pose_id": "P001_front", "garment_id": "SPR-0001"})
    assert kept == [tmp_path / "results" / JOB / POSE_NAME,
                    tmp_path / "results" / JOB / GARMENT_NAME]
    # 统一转成 PNG，并按 1024 上限缩过 —— 附属物只用来回看，不存原图。
    with Image.open(kept[0]) as pose:
        assert pose.format == "PNG" and pose.size == (768, 1024)
    with Image.open(kept[1]) as garment:
        assert garment.format == "PNG" and garment.size == (683, 1024)


def test_keep_library_skips_the_kind_that_was_not_selected(tmp_path):
    install_library(tmp_path, "poses", "P001_front")
    images = store(tmp_path)
    kept = images.keep_library(JOB, {"pose_id": "P001_front"})
    assert kept == [tmp_path / "results" / JOB / POSE_NAME]
    assert not (tmp_path / "results" / JOB / GARMENT_NAME).exists()


def test_keep_library_is_silent_for_a_vanished_item(tmp_path):
    """素材被删掉不该让任务发布失败，只是历史里没有那张图。"""
    install_library(tmp_path, "poses", "P001_front")
    images = store(tmp_path)
    assert images.keep_library(JOB, {"pose_id": "P404_gone"}) == []


def test_keep_library_is_silent_without_a_library(tmp_path):
    images = store(tmp_path)
    assert images.keep_library(JOB, {"pose_id": "P001_front"}) == []


def test_keep_library_ignores_jobs_without_a_library_selection(tmp_path):
    images = store(tmp_path)
    assert images.keep_library(JOB, {"prompt": "一只猫"}) == []
    assert images.keep_library(JOB, None) == []


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *params):
        class Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

        if sql.strip().startswith("SELECT * FROM artifacts"):
            return Cursor(self.rows)
        return Cursor([])


def job_folder(tmp_path):
    folder = tmp_path / "results" / JOB
    folder.mkdir(parents=True)
    artifact = folder / f"{ARTIFACT}.png"
    artifact.write_bytes(png((64, 64)))
    return folder, artifact


def test_reconcile_keeps_the_reference_copy(tmp_path):
    """reference.png 不是"多余文件"，reconcile 不许把它隔离走。"""
    images = store(tmp_path)
    folder, artifact = job_folder(tmp_path)
    (folder / REFERENCE_NAME).write_bytes(png())
    rows = [{"job_id": JOB, "artifact_id": ARTIFACT, "size_bytes": artifact.stat().st_size}]
    images.reconcile(FakeDB(rows))
    assert artifact.is_file()
    assert (folder / REFERENCE_NAME).is_file()
    assert not any(images.quarantine.iterdir())


def test_reconcile_keeps_every_attachment(tmp_path):
    """姿势图、服装图同样是附属物，任何一份都不许被隔离。"""
    images = store(tmp_path)
    folder, artifact = job_folder(tmp_path)
    for name in ATTACHMENTS:
        (folder / name).write_bytes(png((96, 96)))
    rows = [{"job_id": JOB, "artifact_id": ARTIFACT, "size_bytes": artifact.stat().st_size}]
    images.reconcile(FakeDB(rows))
    assert all((folder / name).is_file() for name in ATTACHMENTS)
    assert not any(images.quarantine.iterdir())


def test_reconcile_still_isolates_real_strays(tmp_path):
    images = store(tmp_path)
    folder, artifact = job_folder(tmp_path)
    stray = folder / "stray.png"
    stray.write_bytes(png((32, 32)))
    rows = [{"job_id": JOB, "artifact_id": ARTIFACT, "size_bytes": artifact.stat().st_size}]
    images.reconcile(FakeDB(rows))
    assert artifact.is_file() and not stray.exists()
    assert list(images.quarantine.iterdir())
