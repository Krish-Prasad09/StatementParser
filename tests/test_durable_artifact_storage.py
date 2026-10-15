import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models import ExtractionResult
from persistence import StatementStore
from learner import FormatLearner


class DurableArtifactStorageTests(unittest.TestCase):
    def test_template_and_review_file_survive_store_recreation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "state.db")
            source_path = os.path.join(temp_dir, "incoming", "statement.pdf")
            os.makedirs(os.path.dirname(source_path))
            source_bytes = b"%PDF-1.4 durable test attachment"
            Path(source_path).write_bytes(source_bytes)

            store = StatementStore(db_path)
            result = ExtractionResult(
                file=source_path,
                format_id="sample_format",
                status="needs_review",
                issues=["manual_review"],
                sha256_hash=hashlib.sha256(source_bytes).hexdigest(),
            )
            store.save_result(result)
            template = {"format_id": "sample_format", "approved": False, "fields": {"provider": "Sample"}}
            store.save_template(template)

            Path(source_path).unlink()
            reopened = StatementStore(db_path)
            self.assertEqual(reopened.get_template("sample_format"), template)
            review_row = reopened.get_needs_review()[0]
            restored_path = review_row["file_path"]
            self.assertTrue(os.path.isfile(restored_path))
            self.assertEqual(Path(restored_path).read_bytes(), source_bytes)

    def test_failed_learning_loads_from_postgres_target_without_local_file(self):
        db_url = "postgresql://review-test.invalid/sample"
        expected = {"format_id": "sample_format", "fingerprint_hash": "abc123"}

        class FakeStore:
            def __init__(self, _db_path):
                pass

            def get_failed_learnings(self):
                return [expected]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("persistence.StatementStore", FakeStore), patch(
                "persistence._is_pg_target", return_value=(True, db_url)
            ):
                learner = FormatLearner(
                    templates_dir=os.path.join(temp_dir, "templates"),
                    db_path=db_url,
                )

        self.assertIn(("sample_format", "abc123"), learner.failed_formats)


if __name__ == "__main__":
    unittest.main()
