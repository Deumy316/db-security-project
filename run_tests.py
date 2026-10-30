"""전체 테스트: 실제 MySQL을 차단하고 인증 감사 저장소는 임시 폴더로 격리한다."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def main():
    import server

    with tempfile.TemporaryDirectory() as directory:
        with patch.dict(server.app.config, {"AUTH_AUDIT_PATH": str(Path(directory) / "audit.sqlite3")}), \
                patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real MySQL forbidden")):
            suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent / "tests"))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
