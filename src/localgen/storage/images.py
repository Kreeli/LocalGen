import logging
import os
import shutil
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image

from localgen.config import Settings, atomic_write, guarded
from localgen.jobs.models import AppError

logger = logging.getLogger(__name__)


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
                    if path not in expected[folder]:
                        self.isolate(path)
            except OSError:
                self.isolate(folder)
        for path in self.staging.iterdir():
            self.isolate(path)

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
