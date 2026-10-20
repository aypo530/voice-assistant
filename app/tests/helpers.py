import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.GEMINI_API_KEY = ""                       # тесты не ходят в сеть
config.DATA_FILE = Path(tempfile.mkdtemp()) / "test.csv"
config.MULTI_USER = False
