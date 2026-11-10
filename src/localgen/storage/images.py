import logging
import os
import shutil
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image

from localgen.config import Settings, atomic_write, guarded
from localgen.imaging import open_rgb, preview
from localgen.jobs.models import AppError

logger = logging.getLogger(__name__)
# 任务附属物：引擎实际吃到的参考图（裁剪到输出比例后的那一份）。
REFERENCE_NAME = "reference.png"
# 任务附属物：换姿势 / 换装任务当时选中的图库原图。图库是第三方只读素材，
# 随时可能被重新导入或删掉；历史记录必须能永久说清"当时选的是哪一张"，
# 所以在发布成功那一刻抄一份进任务目录（统一转 PNG，界面按固定名字找）。
POSE_NAME = "pose.png"
GARMENT_NAME = "garment.png"
ATTACHMENTS = (REFERENCE_NAME, POSE_NAME, GARMENT_NAME)
LIBRARY_SEEDS = (("pose_id", "poses", POSE_NAME), ("garment_id", "garments", GARMENT_NAME))
# 附属图只用来"回看选了哪张"，不需要原始 2048 px；1024 已足够看清姿势与服装。
LIBRARY_LIMIT = 1024


def uuid_name(value):
    parsed = UUID(str(value))
    if str(parsed) != str(value):
        raise ValueError("无效文件标识")
    return str(parsed)


class ImageStore:
    def __init__(self, data: Path, settings: Settings):
        self.data = data
        self.limit = settings.max_image_mib * 2**20
        self.max_pixels = settings.max_pixels
        self.results = guarded(data / "results")
        self.staging = guarded(data / "staging")
        self.quarantine = guarded(data / "quarantine")
        for folder in (self.results, self.staging, self.quarantine):
            folder.mkdir(exist_ok=True)
        # 图库索引要懒加载：只有换姿势/换装任务才需要它，普通生图不该为一次
        # 索引重建埋单（姿势 1439 条 + 服装 1407 条的 JSON 不小）。
        self._libraries = None

    def path(self, job_id, artifact_id):
        return guarded(self.results / uuid_name(job_id) / f"{uuid_name(artifact_id)}.png")

    def isolate(self, path: Path):
        try:
            # Rename the entry itself. Never open or traverse it.
            os.replace(path, guarded(self.quarantine / f"{uuid4().hex}-{path.name}"))
        except OSError:
            logger.warning("QUARANTINE_FAILED entry=%s", path.name)

    def reconcile(self, db):
        expected = {}
        for row in db.execute("SELECT * FROM artifacts").fetchall():
            job_id, artifact_id = row["job_id"], row["artifact_id"]
            try:
                path = self.path(job_id, artifact_id)
                expected.setdefault(path.parent, set()).add(path)
                available = path.is_file() and path.stat().st_size == row["size_bytes"]
            except (OSError, ValueError):
                available = False
            db.execute("UPDATE artifacts SET available=? WHERE artifact_id=?", int(available), artifact_id)
            if not available:
                logger.warning("ARTIFACT_MISSING job=%s artifact=%s", job_id, artifact_id)
        for folder in self.results.iterdir():
            if folder not in expected:
                self.isolate(folder)
                continue
            try:
                for path in guarded(folder).iterdir():
                    # reference.png / pose.png / garment.png 是任务附属物（引擎吃到的
                    # 参考图 + 当时选中的姿势、服装），不是多余文件，绝不隔离。
                    if path not in expected[folder] and path.name not in ATTACHMENTS:
                        self.isolate(path)
            except OSError:
                self.isolate(folder)
        for path in self.staging.iterdir():
            self.isolate(path)

    def keep_reference(self, job_id, parameters=None):
        """把引擎真正吃到的那张参考图转存成任务附属物（`results/<job_id>/reference.png`）。

        两个理由：`gateway-input/` 迟早要加 TTL，届时历史里就说不清当初喂了什么；
        而引擎目录在任务发布后立刻被清理，所以只有这一刻能搬走。

        引擎侧的命名不是固定的 `input.png`：`sdcpp._stage` 按
        `{prefix}ref{index}-{kind}.png` 逐个落盘（单图生图恰好只有一张、且历史
        代码写死 input.png 时能对上；两段式 pose-swap 则是 ref0-person.png 与
        ref1-pose.png，永远不会有 input.png）。所以这里按模式匹配取"人物参考
        图"：优先 ref*-person，其次 ref0，再次 input.*，最后才退回任意单张。
        """
        try:
            job = uuid_name(job_id)
            folder = guarded(self.data / "comfy-output" / job)
            if not folder.is_dir():
                return None
            source = self._pick_person_reference(folder)
            if source is None:
                return None
            target = guarded(self.results / job / REFERENCE_NAME)
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            return target
        except (OSError, ValueError):
            logger.warning("REFERENCE_KEEP_FAILED job=%s", job_id)
            return None

    @staticmethod
    def _pick_person_reference(folder):
        """从引擎目录里挑出"人物参考图"，找不到返回 None。

        pose-swap 这类多参考任务里，人物照片是信息主体，姿势骨骼/服装平铺只是
        约束；历史回看要的是前者。顺序写成显式优先级，别依赖 iterdir() 的顺序。
        """
        try:
            files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
        except OSError:
            return None
        if not files:
            return None
        # 1) ref{N}-person.png —— 两段式任务的人物图
        person = sorted((p for p in files if p.stem.endswith("-person")), key=lambda p: p.name)
        if person:
            return person[0]
        # 2) 单图生图的兼容路径
        legacy = folder / "input.png"
        if legacy.is_file():
            return legacy
        # 3) 只有一个参考图时，它就是人物图
        refs = sorted((p for p in files if p.stem.startswith("ref")), key=lambda p: p.name)
        if len(refs) == 1:
            return refs[0]
        return None

    def keep_library(self, job_id, parameters):
        """把换姿势 / 换装当时选中的图库原图抄进任务目录（pose.png / garment.png）。

        `pose_id` / `garment_id` 只是图库里的一个键，素材被重新导入或删掉之后，
        历史记录就再也说不清"当时选的是哪一张"（用户反馈：历史里看不到姿势、
        服装的图）。所以在发布成功那一刻落一份副本，统一转成 PNG 并按固定名字
        存，界面按名字直读，不需要再回图库查一遍。
        """
        kept = []
        for field, kind, name in LIBRARY_SEEDS:
            item_id = (parameters or {}).get(field)
            if not item_id:
                continue
            target = self._keep_library_asset(job_id, kind, str(item_id), name)
            if target is not None:
                kept.append(target)
        return kept

    def _keep_library_asset(self, job_id, kind, item_id, name):
        try:
            job = uuid_name(job_id)
            content = self._library().source_path(kind, item_id).read_bytes()
            # 附属图只用来回看选了哪张，不需要原始 2048 px：缩到 1024 再存，
            # 一条任务多几十 KB 而不是几百 KB。
            image = preview(open_rgb(content), LIBRARY_LIMIT)
            buffer = BytesIO()
            image.save(buffer, "PNG", optimize=True)
            target = guarded(self.results / job / name)
            target.parent.mkdir(exist_ok=True)
            atomic_write(target, buffer.getvalue())
            return target
        except (OSError, ValueError, AppError, Image.DecompressionBombError):
            logger.warning("LIBRARY_KEEP_FAILED job=%s kind=%s item=%s", job_id, kind, item_id)
            return None

    def _library(self):
        """懒加载图库索引：普通生图任务不该为它付一次 JSON 解析。"""
        if self._libraries is None:
            from localgen.storage.libraries import LibraryStore
            self._libraries = LibraryStore(self.data)
        return self._libraries

    def stage(self, content: bytes, width: int, height: int):
        if not content or len(content) > self.limit:
            raise AppError(422, "INVALID_IMAGE", "结果图片大小超限")
        try:
            with Image.open(BytesIO(content), formats=["PNG"]) as image:
                if (image.size != (width, height) or width * height > self.max_pixels
                        or getattr(image, "is_animated", False)):
                    raise ValueError("图片尺寸或帧数无效")
                image.verify()
            with Image.open(BytesIO(content), formats=["PNG"]) as image:
                image.load()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise AppError(422, "INVALID_IMAGE", "引擎结果不是完整且符合尺寸的单张 PNG") from exc
        artifact_id = str(uuid4())
        path = guarded(self.staging / f"{artifact_id}.png")
        atomic_write(path, content)
        return artifact_id, path

    def publish(self, db, job_id, artifact_id, temp, width, height):
        final = self.path(job_id, artifact_id)
        final.parent.mkdir(exist_ok=True)
        size = temp.stat().st_size
        os.replace(temp, final)
        if not db.succeed(job_id, artifact_id, width, height, size):
            self.isolate(final)
            return False
        return True

    def purge(self, job_id):
        try:
            folder = guarded(self.results / uuid_name(job_id))
            if folder.is_dir():
                shutil.rmtree(folder)
        except (OSError, ValueError):
            logger.warning("ARTIFACT_PURGE_FAILED job=%s", job_id)

    def download(self, db, job_id: UUID, artifact_id: UUID):
        result = db.execute(
            """SELECT a.size_bytes FROM artifacts a JOIN jobs j ON j.job_id=a.job_id
               WHERE a.job_id=? AND a.artifact_id=? AND j.status='succeeded' AND a.available=1""",
            str(job_id), str(artifact_id),
        ).fetchone()
        if result:
            try:
                file = self.path(job_id, artifact_id)
                if file.is_file() and file.stat().st_size == result["size_bytes"]:
                    return file
            except OSError:
                pass
            db.execute("UPDATE artifacts SET available=0 WHERE artifact_id=?", str(artifact_id))
            logger.warning("ARTIFACT_MISSING job=%s artifact=%s", job_id, artifact_id)
        raise AppError(404, "ARTIFACT_NOT_FOUND", "图片不存在或已丢失")
