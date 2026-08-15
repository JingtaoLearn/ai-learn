import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECTS = Path(__file__).resolve().parents[2]
TOMBSTONE = PROJECTS / "finance" / "retired_register_report.py"


class RetiredFinanceRegistryTests(unittest.TestCase):
    def test_tombstone_exits_two_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            result = subprocess.run(
                ["python3", str(TOMBSTONE)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(before, after)
            self.assertIn("content-hub", result.stderr)


if __name__ == "__main__":
    unittest.main()
