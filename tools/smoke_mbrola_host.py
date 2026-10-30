"""Stream a short phrase through the x86 MBROLA host and optionally save WAV."""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import wave


MAGIC = 0x484B544D
READY = 1
AUDIO = 2
DONE = 3
CANCELLED = 4
ERROR = 5
SAMPLE_RATE = 6
RESPONSE = 7
SET_VOICE = 11
SPEAK = 13
QUIT = 0xFFFF

HEADER = struct.Struct("<IIII")
AUDIO_META = struct.Struct("<II")
AUDIO_MARKER = struct.Struct("<iI")


def read_exact(stream, size):
	parts = []
	while size:
		part = stream.read(size)
		if not part:
			raise EOFError("MBROLA host exited unexpectedly")
		parts.append(part)
		size -= len(part)
	return b"".join(parts)


def send(process, message_type, generation=0, payload=b""):
	process.stdin.write(HEADER.pack(MAGIC, message_type, generation, len(payload)) + payload)
	process.stdin.flush()


def receive(process):
	magic, message_type, generation, size = HEADER.unpack(read_exact(process.stdout, HEADER.size))
	if magic != MAGIC or size > 16 * 1024 * 1024:
		raise RuntimeError("Invalid response packet from MBROLA host")
	return message_type, generation, read_exact(process.stdout, size) if size else b""


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--engine-dir", required=True, help="Existing add-on synthDrivers folder with its x86 DLLs and espeak-data")
	parser.add_argument("--host", default=os.path.join(os.path.dirname(__file__), "..", "build", "bin", "espeakMbrolaHost.exe"))
	parser.add_argument("--voice", default="mb-en1")
	parser.add_argument("--text", default='<mark name="42" />Hello <mark name="43" />world.')
	parser.add_argument("--output", help="Optional output WAV path")
	parser.add_argument("--trace-events", action="store_true", help="Print eSpeak callback events for diagnosing mark delivery")
	args = parser.parse_args()

	host = os.path.abspath(args.host)
	engine_dir = os.path.abspath(args.engine_dir)
	if not os.path.isfile(host):
		parser.error("Host executable not found: %s" % host)
	if not os.path.isfile(os.path.join(engine_dir, "espeak.dll")):
		parser.error("espeak.dll not found in engine folder: %s" % engine_dir)
	if not os.path.isfile(os.path.join(engine_dir, "mbrola.dll")):
		parser.error("mbrola.dll not found in engine folder: %s" % engine_dir)

	creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
	environment = os.environ.copy()
	if args.trace_events:
		environment["ESPEAK_MB_TRACE_EVENTS"] = "1"
	process = subprocess.Popen(
		[host, engine_dir],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		bufsize=0,
		creationflags=creationflags,
		env=environment,
	)
	pcm = bytearray()
	markers = []
	rate = 0
	try:
		message_type, _generation, payload = receive(process)
		if message_type == ERROR:
			raise RuntimeError(payload.decode("utf-8", "replace"))
		if message_type != READY:
			raise RuntimeError("Expected READY from host, got message %d" % message_type)
		ready = json.loads(payload.decode("utf-8"))
		voices = [voice["id"] for voice in ready.get("voices", [])]
		print("Host initialized: %s at %s Hz" % (ready.get("info", "eSpeak"), ready.get("sampleRate")))
		print("Discovered %d eSpeak voices" % len(voices))
		if args.voice not in voices:
			raise RuntimeError("Voice %r is unavailable; first voices: %s" % (args.voice, ", ".join(voices[:12])))

		send(process, SET_VOICE, 1, args.voice.encode("utf-8"))
		message_type, _request, payload = receive(process)
		if message_type != RESPONSE:
			raise RuntimeError("Expected voice response, got message %d" % message_type)
		voice_result = json.loads(payload.decode("utf-8"))
		if voice_result.get("status") != 0:
			raise RuntimeError("eSpeak rejected voice %s: %s" % (args.voice, voice_result))

		send(process, SPEAK, 2, args.text.encode("utf-8"))
		while True:
			message_type, generation, payload = receive(process)
			if generation not in (0, 2):
				continue
			if message_type == SAMPLE_RATE:
				rate = struct.unpack("<i", payload)[0]
			elif message_type == AUDIO:
				packet_rate, marker_count = AUDIO_META.unpack_from(payload)
				if rate and rate != packet_rate:
					raise RuntimeError("Sample rate changed mid-utterance (%d to %d)" % (rate, packet_rate))
				rate = packet_rate
				offset = AUDIO_META.size
				for _ in range(marker_count):
					index, byte_offset = AUDIO_MARKER.unpack_from(payload, offset)
					markers.append((index, len(pcm) + byte_offset))
					offset += AUDIO_MARKER.size
				pcm.extend(payload[offset:])
			elif message_type == ERROR:
				raise RuntimeError(payload.decode("utf-8", "replace"))
			elif message_type in (DONE, CANCELLED):
				break

		if not rate or not pcm:
			raise RuntimeError("Synthesis completed without any PCM output")
		print("Synthesis OK: %d PCM bytes at %d Hz; index markers: %s" % (len(pcm), rate, markers))
		if args.output:
			with wave.open(args.output, "wb") as output:
				output.setnchannels(1)
				output.setsampwidth(2)
				output.setframerate(rate)
				output.writeframes(pcm)
			print("Wrote %s" % args.output)
	finally:
		try:
			send(process, QUIT)
		except (OSError, ValueError):
			pass
		try:
			process.wait(timeout=3)
		except subprocess.TimeoutExpired:
			process.terminate()
			process.wait(timeout=2)
		process.stdin.close()
		process.stdout.close()
		trace = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
		if trace:
			print(trace, file=sys.stderr, end="")
		if process.stderr:
			process.stderr.close()


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print("MBROLA host smoke check failed: %s" % error, file=sys.stderr)
		sys.exit(1)
