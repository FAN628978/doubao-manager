import sqlite3

from doubao_manager.web.schema import ensure_schema


def test_schema_migrates_delivery_columns_before_creating_index(tmp_path):
    dbfile = tmp_path / "legacy-schema.sqlite3"

    with sqlite3.connect(dbfile) as conn:
        conn.executescript(
            """
            CREATE TABLE video_web_jobs(
                job_id TEXT PRIMARY KEY,
                ratio TEXT,
                duration INTEGER,
                idempotency_key TEXT UNIQUE,
                video_url TEXT DEFAULT '',
                web_page_url TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                result_push_url TEXT DEFAULT '',
                result_push_token TEXT DEFAULT '',
                result_pushed_at TEXT DEFAULT '',
                result_push_error TEXT DEFAULT ''
            );
            INSERT INTO video_web_jobs(
                job_id, result_push_url, result_push_error
            ) VALUES('legacy_job', 'http://127.0.0.1/callback', 'old push failure');
            """
        )

    def db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        return conn

    ensure_schema(db)

    with db() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(video_web_jobs)")
        }
        assert {
            "delivery_state",
            "delivery_attempts",
            "delivery_next_retry_at",
            "delivery_error",
        } <= columns

        delivery = conn.execute(
            "SELECT delivery_state,delivery_attempts,delivery_error "
            "FROM video_web_jobs WHERE job_id='legacy_job'"
        ).fetchone()
        assert delivery["delivery_state"] == "failed"
        assert delivery["delivery_attempts"] == 1
        assert delivery["delivery_error"] == "old push failure"

        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(video_web_jobs)")
        }
        assert "idx_video_web_delivery" in indexes
