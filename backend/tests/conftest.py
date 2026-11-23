import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix='report-tests-'))
os.environ['DATABASE_URL'] = f'sqlite:///{_TMP / "test.db"}'
os.environ['JWT_SECRET'] = 'test-signing-key-for-report-tests-only-32b'
os.environ['ARTIFACT_DIR'] = str(_TMP / 'artifacts')
os.environ['SEED_DEMO'] = 'true'
