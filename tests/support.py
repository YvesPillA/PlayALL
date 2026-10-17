from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from playall.config import Settings


class IsolatedSettingsMixin:
    """Never construct Settings without an explicit temporary directory."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="playall-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "", "PLAYALL_API_KEY": "", "FACTORIO_PATH": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        detection = patch("playall.config.detect_factorio", return_value="")
        detection.start()
        self.addCleanup(detection.stop)
        self.settings = Settings(self.directory)
