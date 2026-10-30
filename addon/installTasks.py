# -*- coding: UTF-8 -*-
# A part of the eSpeak NG with MBROLA add-on for NVDA.
# Copyright (C) 2023-2024 Felipe Porciuncula Zanabria
# Copyright (C) 2026 Evangelosz Nagy
# This file is covered by the GNU General Public License, version 2.
# See LICENSES/GPL-2.0.txt for details.
#
# Purpose: keep databases and voice data that the user added themselves when the
# add-on is updated, without replacing the files that ship in the new version.

import os
import shutil

import addonHandler
import globalVars

ADDON_NAME = "espeakWitMbrola"

# Folders inside the add-on that users are expected to add files to.
VOICE_PATHS = (
	os.path.join("synthDrivers", "espeak-ng-data", "mbrola"),
	os.path.join("synthDrivers", "espeak-ng-data", "mbrola_ph"),
	os.path.join("synthDrivers", "espeak-ng-data", "voices", "mb"),
)

# Layout used by the upstream 2.0 add-on (classic eSpeak 1.48 data folder).
LEGACY_PATHS = (
	os.path.join("synthDrivers", "espeak-data", "mbrola"),
	os.path.join("synthDrivers", "espeak-data", "mbrola_ph"),
	os.path.join("synthDrivers", "espeak-data", "voices", "mb"),
)


def _addonPath(name):
	return os.path.join(globalVars.appArgs.configPath, "addons", name)


def _copyMissing(source, destination):
	"""Copy files that do not already exist in destination.

	Existing files are never overwritten, so the databases and descriptors that
	ship with the new version always win, while user-added voices survive.
	"""
	copied = 0
	if not os.path.isdir(source):
		return copied
	for root, _dirs, files in os.walk(source):
		relative = os.path.relpath(root, source)
		target = destination if relative == "." else os.path.join(destination, relative)
		try:
			os.makedirs(target, exist_ok=True)
		except OSError:
			continue
		for name in files:
			path = os.path.join(root, name)
			existing = os.path.join(target, name)
			if os.path.exists(existing):
				continue
			try:
				shutil.copy2(path, existing)
				copied += 1
			except OSError:
				pass
	return copied


def onInstall():
	installed = _addonPath(ADDON_NAME)
	pending = installed + addonHandler.ADDON_PENDINGINSTALL_SUFFIX
	if not os.path.isdir(installed) or not os.path.isdir(pending):
		return
	total = 0
	for relative, legacy in zip(VOICE_PATHS, LEGACY_PATHS):
		target = os.path.join(pending, relative)
		total += _copyMissing(os.path.join(installed, relative), target)
		total += _copyMissing(os.path.join(installed, legacy), target)
	if total:
		print("eSpeak with MBROLA: kept %d user voice file(s) across the update." % total)
