"""Always collect tests against the example, never an installed user's config."""
from pathlib import Path
from unittest.mock import patch

_local = Path(__file__).resolve().parent/'config.local.json'
_original = Path.is_file
with patch.object(Path, 'is_file', lambda path: False if path == _local else _original(path)):
    import settings
