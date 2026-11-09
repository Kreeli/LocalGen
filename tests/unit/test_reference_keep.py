"""ImageStore.keep_reference 与 reconcile 的参考图豁免测试。"""
from io import BytesIO

from PIL import Image

from localgen.config import Settings
from localgen.storage.images import REFERENCE_NAME, ImageStore

JOB = "0f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"


def png(size=(768, 768)):
    buffer = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def store(tmp_path):
    return ImageStore(tmp_path, Settings(engine_mode="mock", min_free_mib=0))


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


def test_reconcile_keeps_the_reference_copy(tmp_path):
    """reference.png 不是"多余文件"，reconcile 不许把它隔离走。"""
    images = store(tmp_path)
    folder = tmp_path / "results" / JOB
    folder.mkdir(parents=True)
    artifact = folder / "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901.png"
    artifact.write_bytes(png((64, 64)))
    (folder / REFERENCE_NAME).write_bytes(png())
    rows = [{"job_id": JOB, "artifact_id": "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901",
             "size_bytes": artifact.stat().st_size}]
    images.reconcile(FakeDB(rows))
    assert artifact.is_file()
    assert (folder / REFERENCE_NAME).is_file()
    assert not any(images.quarantine.iterdir())


def test_reconcile_still_isolates_real_strays(tmp_path):
    images = store(tmp_path)
    folder = tmp_path / "results" / JOB
    folder.mkdir(parents=True)
    artifact = folder / "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901.png"
    artifact.write_bytes(png((64, 64)))
    stray = folder / "stray.png"
    stray.write_bytes(png((32, 32)))
    rows = [{"job_id": JOB, "artifact_id": "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901",
             "size_bytes": artifact.stat().st_size}]
    images.reconcile(FakeDB(rows))
    assert artifact.is_file() and not stray.exists()
    assert list(images.quarantine.iterdir())
