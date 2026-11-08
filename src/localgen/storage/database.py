import base64
import hashlib
import hmac
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from localgen.config import guarded
from localgen.jobs.models import AppError, Artifact, ErrorDetail, Job, JobPage, JobQuery, Progress, TERMINAL


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path, token: str):
        for suffix in ("", "-wal", "-shm", "-journal"):
            guarded(path.with_name(path.name + suffix))
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=3)
        self.connection.row_factory = sqlite3.Row
        self.key = token.encode()
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=3000")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE jobs (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL CHECK(status IN ('queued','submitting','running','cancelling',
                            'succeeded','failed','cancelled','interrupted')),
                        workflow_id TEXT NOT NULL,
                        workflow_revision TEXT NOT NULL,
                        template_sha256 TEXT NOT NULL,
                        parameters TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        progress TEXT,
                        error TEXT,
                        prompt_id TEXT,
                        idem_key TEXT UNIQUE,
                        request_hash TEXT
                    );
                    CREATE INDEX job_status_seq ON jobs(status, seq);
                    CREATE UNIQUE INDEX one_active_job ON jobs((1))
                        WHERE status IN ('submitting','running','cancelling');
                    CREATE TABLE artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
                        width INTEGER NOT NULL,
                        height INTEGER NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        available INTEGER NOT NULL DEFAULT 1
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            elif version != 1:
                raise RuntimeError(f"数据库版本 {version} 与此 LocalGen 版本不兼容")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def execute(self, statement, *args):
        return self.connection.execute(statement, args)

    def row(self, job_id):
        row = self.execute("SELECT * FROM jobs WHERE job_id=?", str(job_id)).fetchone()
        if row is None:
            raise AppError(404, "JOB_NOT_FOUND", "找不到此任务")
        return row

    def job(self, row) -> Job:
        job_id = row["job_id"]
        result = self.execute("SELECT * FROM artifacts WHERE job_id=?", job_id).fetchone()
        artifacts = []
        if result:
            artifact_id = result["artifact_id"]
            artifacts.append(Artifact(
                artifact_id=artifact_id, width=result["width"], height=result["height"],
                size_bytes=result["size_bytes"], available=bool(result["available"]),
                download_path=f"/api/v1/jobs/{job_id}/artifacts/{artifact_id}",
            ))
        return Job(
            job_id=job_id, status=row["status"], workflow_id=row["workflow_id"],
            workflow_revision=row["workflow_revision"], parameters=json.loads(row["parameters"]),
            created_at=row["created_at"], started_at=row["started_at"], finished_at=row["finished_at"],
            progress=Progress.model_validate_json(row["progress"]) if row["progress"] else None,
            error=ErrorDetail.model_validate_json(row["error"]) if row["error"] else None,
            artifacts=artifacts,
        )

    def enqueue(self, request_hash, key, capacity, prepare):
        with self.transaction():
            if key is not None:
                previous = self.execute("SELECT * FROM jobs WHERE idem_key=?", key).fetchone()
                if previous:
                    if not hmac.compare_digest(previous["request_hash"], request_hash):
                        raise AppError(409, "IDEMPOTENCY_CONFLICT", "此重试键已用于另一份请求")
                    return self.job(previous)
            spec, parameters = prepare()
            count = self.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if count >= capacity:
                raise AppError(429, "QUEUE_FULL", "等待队列已满", True)
            job_id = str(uuid4())
            self.execute(
                """INSERT INTO jobs (job_id,status,workflow_id,workflow_revision,template_sha256,
                    parameters,created_at,idem_key,request_hash) VALUES (?,'queued',?,?,?,?,?,?,?)""",
                job_id, spec.workflow_id, spec.revision, spec.template_sha256,
                json.dumps(parameters, ensure_ascii=False), now(), key, request_hash,
            )
            return self.job(self.row(job_id))

    def claim(self, eligible):
        with self.transaction():
            row = self.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY seq LIMIT 1").fetchone()
            if row and eligible(row) and self.execute(
                """UPDATE jobs SET status='submitting', started_at=? WHERE job_id=? AND status='queued'
                AND NOT EXISTS(SELECT 1 FROM jobs WHERE status IN ('submitting','running','cancelling'))""",
                now(), row["job_id"],
            ).rowcount:
                return self.row(row["job_id"])
        return None

    def running(self, job_id, prompt_id):
        self.execute(
            """UPDATE jobs SET prompt_id=?, status=CASE WHEN status='submitting' THEN 'running'
                ELSE status END WHERE job_id=? AND status IN ('submitting','cancelling')""",
            prompt_id, job_id,
        )

    def progress(self, job_id, progress):
        self.execute("UPDATE jobs SET progress=? WHERE job_id=? AND status='running'",
                     progress.model_dump_json(), job_id)

    def cancel(self, job_id):
        with self.transaction():
            row = self.row(job_id)
            match row["status"]:
                case "queued":
                    self.execute(
                        "UPDATE jobs SET status='cancelled', finished_at=? WHERE job_id=? AND status='queued'",
                        now(), str(job_id),
                    )
                case "submitting" | "running":
                    self.execute(
                        "UPDATE jobs SET status='cancelling' WHERE job_id=? AND status IN ('submitting','running')",
                        str(job_id),
                    )
                case "cancelling" | "cancelled":
                    pass
                case _:
                    raise AppError(409, "JOB_TERMINAL", "任务已结束，无法取消")
            return self.job(self.row(job_id))

    def delete(self, job_id):
        job_id = str(job_id)
        with self.transaction():
            row = self.row(job_id)
            if row["status"] not in TERMINAL:
                raise AppError(409, "JOB_ACTIVE", "任务仍在队列或执行中，请先取消")
            self.execute("DELETE FROM artifacts WHERE job_id=?", job_id)
            self.execute("DELETE FROM jobs WHERE job_id=?", job_id)

    def active_row(self):
        return self.execute(
            "SELECT * FROM jobs WHERE status IN ('submitting','running','cancelling') ORDER BY seq LIMIT 1"
        ).fetchone()

    def queued_count(self):
        return self.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]

    def request_stop(self, job_id):
        self.execute("UPDATE jobs SET status='cancelling' WHERE job_id=? AND status IN ('submitting','running')",
                     job_id)

    def finish(self, job_id, status, error=None):
        self.execute(
            """UPDATE jobs SET status=?, finished_at=?, error=? WHERE job_id=?
                AND status IN ('submitting','running','cancelling')""",
            status, now(), error.model_dump_json() if error else None, job_id,
        )

    def succeed(self, job_id, artifact_id, width, height, size):
        with self.transaction():
            if not self.execute(
                "UPDATE jobs SET status='succeeded', finished_at=? WHERE job_id=? AND status='running'",
                now(), job_id,
            ).rowcount:
                return False
            self.execute(
                "INSERT INTO artifacts (artifact_id,job_id,width,height,size_bytes) VALUES (?,?,?,?,?)",
                artifact_id, job_id, width, height, size,
            )
        return True

    def recover(self):
        error = ErrorDetail(code="APP_RESTARTED", message="LocalGen 重启，无法确认原任务结果", retryable=True)
        self.execute(
            """UPDATE jobs SET status='interrupted', finished_at=?, error=?
                WHERE status IN ('submitting','running','cancelling')""", now(), error.model_dump_json(),
        )

    def cursor(self, seq, status):
        payload = str(seq).encode()
        sig = hmac.digest(self.key, f"{seq}:{status or ''}".encode(), hashlib.sha256)[:16]
        return base64.urlsafe_b64encode(payload + b"." + sig).decode().rstrip("=")

    def page(self, query: JobQuery) -> JobPage:
        clauses = []
        values = []
        if query.status:
            clauses.append("status=?")
            values.append(query.status)
        if query.cursor:
            try:
                decoded = base64.urlsafe_b64decode(query.cursor + "=" * (-len(query.cursor) % 4))
                seq = int(decoded.split(b".", 1)[0])
                if not 0 < seq < 2**63 or not hmac.compare_digest(query.cursor, self.cursor(seq, query.status)):
                    raise ValueError
            except (ValueError, TypeError):
                raise AppError(422, "INVALID_CURSOR", "分页游标无效") from None
            clauses.append("seq < ?")
            values.append(seq)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.execute(f"SELECT * FROM jobs{where} ORDER BY seq DESC LIMIT ?",
                            *values, query.limit + 1).fetchall()
        items = [self.job(row) for row in rows[:query.limit]]
        cursor = self.cursor(rows[query.limit - 1]["seq"], query.status) if len(rows) > query.limit else None
        return JobPage(items=items, next_cursor=cursor)
