# dmgbuild settings for VoiceCut's disk image: the app on the left, an
# Applications shortcut on the right, and a background that says what to do.
#
#   dmgbuild -s dmg_settings.py -D app=VoiceCut.app -D background=bg.png \
#     -D icon=VoiceCut.icns "VoiceCut" VoiceCut.dmg
#
# dmgbuild writes the window layout (.DS_Store) itself, without scripting the
# Finder, so the image builds the same way on any Mac, CI included.
# ``defines`` is provided by dmgbuild.
import os.path

application = defines["app"]  # noqa: F821
name = os.path.basename(application)

format = "ULMO"  # LZMA: the smallest image macOS 10.15 and later can open
filesystem = "HFS+"
files = [application]
symlinks = {"Applications": "/Applications"}
icon = defines.get("icon")  # noqa: F821
background = defines["background"]  # noqa: F821  (dmgbuild adds its @2x twin)

window_rect = ((200, 160), (660, 420))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
icon_size = 128
text_size = 13
icon_locations = {name: (165, 205), "Applications": (495, 205)}
