"""Global consants for the timeline viewer application.

Copyright (c) Peter Triesberger
For further information see https://codeberg.org/peter88213/timeline-view-tk
License: GNU GPLv3 (https://www.gnu.org/licenses/gpl-3.0.en.html)
"""
from pathlib import Path

from tlv.tlv_locale import _

HELP_URL = _('https://codeberg.org/peter88213/timeline-view-tk/src/branch/main/docs/help')
HOME_URL = 'https://codeberg.org/peter88213/timeline-view-tk/'

HOME_DIR = str(Path.home()).replace('\\', '/')
INSTALL_DIR = f'{HOME_DIR}/.tlviewer'
