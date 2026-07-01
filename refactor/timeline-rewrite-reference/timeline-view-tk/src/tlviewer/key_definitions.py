"""Provide platform-dependent key definitions.

Copyright (c) Peter Triesberger
For further information see https://codeberg.org/peter88213/timeline-view-tk
License: GNU GPLv3 (https://www.gnu.org/licenses/gpl-3.0.en.html)
"""
from tlv.tlv_locale import _
from tlv.platform.platform_settings import PLATFORM

if PLATFORM == 'mac':
    KEY_OPEN_PROJECT = ('<Command-o>', 'Cmd-O')
    KEY_RELOAD_PROJECT = ('<Command-r>', 'Cmd-R')
    KEY_SAVE_AS = ('<Command-S>', 'Cmd-Shift-S')
    KEY_SAVE_PROJECT = ('<Command-s>', 'Cmd-S')
else:
    KEY_OPEN_PROJECT = ('<Control-o>', f'{_("Ctrl")}-O')
    KEY_RELOAD_PROJECT = ('<Control-r>', f'{_("Ctrl")}-R')
    KEY_SAVE_AS = ('<Control-S>', f'{_("Ctrl")}-{_("Shift")}-S')
    KEY_SAVE_PROJECT = ('<Control-s>', f'{_("Ctrl")}-S')

