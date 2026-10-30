"""Exercise the NVDA-side MBROLA bridge against real DLLs with a fake WavePlayer."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
import threading
import types
import wave


class _WavePlayer:
	instances = []

	def __init__(self, channels, samplesPerSec, bitsPerSample, outputDevice):
		self.samplesPerSec = samplesPerSec
		self.data = bytearray()
		self.closed = False
		self.instances.append(self)

	def feed(self, data, size=None, onDone=None):
		if size is not None:
			data = data[:size]
		self.data.extend(data)
		if onDone:
			onDone()

	def idle(self):
		return

	def pause(self, switch):
		return

	def stop(self):
		return

	def close(self):
		self.closed = True


def _installNvdaStubs():
	config = types.ModuleType("config")
	config.conf = {"audio": {"outputDevice": None}}
	sys.modules["config"] = config

	nvwave = types.ModuleType("nvwave")
	nvwave.WavePlayer = _WavePlayer
	sys.modules["nvwave"] = nvwave

	logHandler = types.ModuleType("logHandler")
	logHandler.log = logging.getLogger("mbrola-bridge-smoke")
	sys.modules["logHandler"] = logHandler


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--engine-dir", required=True, help="Installed add-on synthDrivers folder with x86 DLLs and espeak-data")
	parser.add_argument("--host", default=os.path.join(os.path.dirname(__file__), "..", "build", "bin", "espeakMbrolaHost.exe"))
	parser.add_argument("--voice", default="mb-en1")
	parser.add_argument("--text", default='<mark name="42" />Hello <mark name="43" />world.')
	parser.add_argument("--output", help="Optional output WAV path")
	args = parser.parse_args()

	engineDir = os.path.abspath(args.engine_dir)
	os.environ["ESPEAK_MB_ENGINE_DIR"] = engineDir
	modulePath = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "addon", "synthDrivers", "_espeak_mb.py"))
	_installNvdaStubs()
	spec = importlib.util.spec_from_file_location("mbrolaBridgeSmoke", modulePath)
	bridge = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(bridge)

	finished = threading.Event()
	events = []
	def onIndex(index):
		events.append(index)
		if index is None:
			finished.set()

	bridge.initialize(onIndex)
	try:
		voices = bridge.getVoiceList()
		available = [voice.identifier.decode("utf-8") for voice in voices]
		print("Bridge initialized; found %d voices" % len(available))
		if args.voice not in available:
			raise RuntimeError("Voice %r is unavailable" % args.voice)
		bridge.setVoiceByName(args.voice)
		bridge.setParameter(bridge.espeakRATE, 175, 0)
		bridge.setParameter(bridge.espeakVOLUME, 100, 0)
		bridge.setParameter(bridge.espeakPITCH, 50, 0)
		bridge.setParameter(bridge.espeakRANGE, 50, 0)
		bridge.speak(args.text)
		if not finished.wait(15):
			raise RuntimeError("NVDA-side bridge did not receive completion")

		players = _WavePlayer.instances
		player = next((item for item in reversed(players) if item.data), None)
		if player is None:
			raise RuntimeError("The NVDA-side WavePlayer received no PCM")
		print("Bridge streamed %d PCM bytes at %d Hz; events: %s" % (len(player.data), player.samplesPerSec, events))
		if args.output:
			with wave.open(args.output, "wb") as output:
				output.setnchannels(1)
				output.setsampwidth(2)
				output.setframerate(player.samplesPerSec)
				output.writeframes(player.data)
			print("Wrote %s" % args.output)
	finally:
		bridge.terminate()


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print("MBROLA bridge smoke check failed: %s" % error, file=sys.stderr)
		sys.exit(1)
