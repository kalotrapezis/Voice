#!/bin/sh
# Refresh icon cache + desktop database (runs on both .deb and .rpm installs).
set -e
[ -x /usr/bin/gtk-update-icon-cache ] && \
  gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
[ -x /usr/bin/update-desktop-database ] && \
  update-desktop-database -q 2>/dev/null || true
exit 0
