"""Source and generated local state paths for the desktop demo."""
import os
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]
STATE = Path(os.environ.get('ZC_DESKTOP_STATE', SOURCE / 'state')).expanduser().resolve()
