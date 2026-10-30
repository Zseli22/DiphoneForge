"""64-bit NVDA-side bridge to the 32-bit eSpeak/MBROLA helper."""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import threading
from collections import namedtuple

import config
import nvwave
from logHandler import log


_MAGIC = 0x484B544D
_READY = 1
_AUDIO = 2
_DONE = 3
_CANCELLED = 4
_ERROR = 5
_SAMPLE_RATE = 6
_RESPONSE = 7

_SET_PARAMETER = 10
_SET_VOICE = 11
_SET_LANGUAGE = 12
_SPEAK = 13
_CANCEL = 14
_QUIT = 0xFFFF

_HEADER = struct.Struct("<IIII")
_AUDIO_META = struct.Struct("<II")
_AUDIO_MARKER = struct.Struct("<iI")
_PARAMETER_DATA = struct.Struct("<ii")
_MAX_PACKET_SIZE = 16 * 1024 * 1024

# eSpeak public parameter IDs and the ranges used by the existing add-on.
espeakSILENCE = 0
espeakRATE = 1
espeakVOLUME = 2
espeakPITCH = 3
espeakRANGE = 4
espeakPUNCTUATION = 5
espeakCAPITALS = 6
espeakWORDGAP = 7
espeakOPTIONS = 8
espeakINTONATION = 9
espeakRESERVED1 = 10
espeakRESERVED2 = 11

minRate = 80
maxRate = 449
minPitch = 0
maxPitch = 99

_Voice = namedtuple("_Voice", ("name", "languages", "identifier"))

_host = None
_onIndexReached = None
_player = None
_playerSampleRate = 0
_voices = []
_currentVoice = None
_parameters = {}
_stateLock = threading.RLock()
_generation = 0
_isTerminating = False
_feeds = {}
_pendingIndexes = {}


class _Feed:
	"""Tracks the audio and index marks of one utterance sent to the host."""

	__slots__ = ("generation", "aborted", "pending", "complete", "doneNotified", "expected", "seen")

	def __init__(self, generation, expected=()):
		self.generation = generation
		self.aborted = False
		#: Number of chunks handed to the player that have not finished playing.
		self.pending = 0
		#: True once the host reported that it has sent all of the audio.
		self.complete = False
		self.doneNotified = False
		#: Index marks the driver put in the text, in order.
		self.expected = list(expected)
		#: Index marks the host has reported so far.
		self.seen = set()


def encodeEspeakString(text):
	return str(text).encode("utf-8")


def decodeEspeakString(data):
	if isinstance(data, bytes):
		return data.decode("utf-8", errors="replace")
	return str(data)


class _HostError(RuntimeError):
	pass


class _Host:
	def __init__(self):
		self.process = None
		self.readerThread = None
		self.writeLock = threading.Lock()
		self.stateLock = threading.RLock()
		self.waiters = {}
		self.nextRequestId = 1
		self.ready = threading.Event()
		self.readyData = None
		self.startupError = None
		self.stopping = False

	@staticmethod
	def _readExact(stream, size):
		parts = []
		while size:
			part = stream.read(size)
			if not part:
				raise EOFError("eSpeak/MBROLA host closed its output")
			parts.append(part)
			size -= len(part)
		return b"".join(parts)

	def start(self):
		moduleDir = os.path.dirname(os.path.abspath(__file__))
		hostPath = os.path.join(moduleDir, "espeakMbrolaHost.exe")
		engineDir = os.environ.get("ESPEAK_MB_ENGINE_DIR") or moduleDir
		if not os.path.isfile(hostPath):
			raise _HostError("espeakMbrolaHost.exe is missing")
		if not os.path.isfile(os.path.join(engineDir, "espeak.dll")):
			raise _HostError("espeak.dll is missing from the configured engine directory")
		try:
			self.process = subprocess.Popen(
				[hostPath, engineDir],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				bufsize=0,
				creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
			)
		except OSError as error:
			raise _HostError("Unable to start espeakMbrolaHost.exe") from error
		self.readerThread = threading.Thread(target=self._reader, name="eSpeak MBROLA host reader", daemon=True)
		self.readerThread.start()
		if not self.ready.wait(30):
			self.stop()
			raise _HostError("The eSpeak/MBROLA host did not become ready")
		if self.startupError:
			message = self.startupError
			self.stop()
			raise _HostError(message)
		return self.readyData

	def _send(self, messageType, generation=0, payload=b""):
		if len(payload) > _MAX_PACKET_SIZE:
			raise _HostError("IPC payload exceeds the configured maximum")
		with self.writeLock:
			process = self.process
			if process is None or process.poll() is not None:
				raise _HostError("The eSpeak/MBROLA host is not running")
			try:
				process.stdin.write(_HEADER.pack(_MAGIC, messageType, generation, len(payload)) + payload)
				process.stdin.flush()
			except (OSError, ValueError) as error:
				raise _HostError("The eSpeak/MBROLA host connection was lost") from error

	def _request(self, messageType, payload, timeout=10):
		with self.stateLock:
			requestId = self.nextRequestId
			self.nextRequestId += 1
			waiter = queue.Queue(maxsize=1)
			self.waiters[requestId] = waiter
		try:
			self._send(messageType, requestId, payload)
			try:
				response = waiter.get(timeout=timeout)
			except queue.Empty as error:
				raise _HostError("The eSpeak/MBROLA host command timed out") from error
			return response
		finally:
			with self.stateLock:
				self.waiters.pop(requestId, None)

	def setVoice(self, voice):
		response = json.loads(self._request(_SET_VOICE, encodeEspeakString(voice)))
		if response.get("status") != 0:
			raise _HostError("eSpeak could not load voice %r (code %s)" % (voice, response.get("status")))
		return response.get("id") or voice

	def setLanguage(self, language):
		try:
			response = json.loads(self._request(_SET_LANGUAGE, encodeEspeakString(language)))
		except Exception:
			if language == "en":
				raise
			response = json.loads(self._request(_SET_LANGUAGE, b"en"))
		if response.get("status") != 0 and language != "en":
			response = json.loads(self._request(_SET_LANGUAGE, b"en"))
		if response.get("status") != 0:
			raise _HostError("eSpeak could not select a voice for language %r" % language)
		return response.get("id")

	def setParameter(self, parameter, value):
		self._send(_SET_PARAMETER, 0, _PARAMETER_DATA.pack(int(parameter), int(value)))

	def speak(self, generation, text):
		self._send(_SPEAK, generation, encodeEspeakString(text))

	def cancel(self, generation):
		try:
			self._send(_CANCEL, generation)
		except _HostError:
			pass

	def _reader(self):
		process = self.process
		try:
			while process is self.process:
				headerData = self._readExact(process.stdout, _HEADER.size)
				magic, messageType, generation, size = _HEADER.unpack(headerData)
				if magic != _MAGIC or size > _MAX_PACKET_SIZE:
					raise _HostError("The eSpeak/MBROLA host returned an invalid packet")
				payload = self._readExact(process.stdout, size) if size else b""
				if messageType == _READY:
					self.readyData = json.loads(payload.decode("utf-8"))
					self.ready.set()
				elif messageType == _ERROR:
					message = payload.decode("utf-8", "replace")
					if generation == 0 and not self.ready.is_set():
						self.startupError = message
						self.ready.set()
					else:
						log.error("eSpeak/MBROLA host: %s", message)
						_notifyDone(generation)
				elif messageType == _RESPONSE:
					with self.stateLock:
						waiter = self.waiters.get(generation)
					if waiter:
						waiter.put(payload.decode("utf-8", "replace"))
				elif messageType == _SAMPLE_RATE:
					if len(payload) == 4:
						_ensurePlayerRate(struct.unpack("<i", payload)[0])
				elif messageType == _AUDIO:
					_handleAudio(generation, payload)
				elif messageType in (_DONE, _CANCELLED):
					_notifyDone(generation, flush=messageType == _DONE)
				else:
					log.debugWarning("Unknown eSpeak/MBROLA host message type %d", messageType)
		except Exception:
			if not self.stopping:
				if not self.ready.is_set():
					self.startupError = "The eSpeak/MBROLA host failed during startup"
					self.ready.set()
				else:
					log.error("eSpeak/MBROLA host reader failed", exc_info=True)
					_notifyDone(_generation)

	def stop(self):
		self.stopping = True
		process = self.process
		self.process = None
		if process is None:
			return
		if process.poll() is None:
			try:
				with self.writeLock:
					process.stdin.write(_HEADER.pack(_MAGIC, _QUIT, 0, 0))
					process.stdin.flush()
			except (OSError, ValueError):
				pass
			try:
				process.wait(timeout=2)
			except subprocess.TimeoutExpired:
				process.terminate()
				try:
					process.wait(timeout=1)
				except subprocess.TimeoutExpired:
					process.kill()
		reader = self.readerThread
		if reader and reader is not threading.current_thread():
			reader.join(timeout=1)
		for stream in (process.stdin, process.stdout):
			try:
				stream.close()
			except Exception:
				pass
		self.readerThread = None


def _newPlayer(sampleRate):
	return nvwave.WavePlayer(
		channels=1,
		samplesPerSec=int(sampleRate),
		bitsPerSample=16,
		outputDevice=config.conf["audio"]["outputDevice"],
	)


def _ensurePlayerRate(sampleRate):
	global _player, _playerSampleRate
	if sampleRate <= 0:
		return
	with _stateLock:
		if _player is not None and _playerSampleRate == sampleRate:
			return
		oldPlayer = _player
		_player = None
		_playerSampleRate = sampleRate
	if oldPlayer is not None:
		try:
			oldPlayer.close()
		except Exception:
			log.debugWarning("Unable to close old eSpeak/MBROLA WavePlayer", exc_info=True)
	with _stateLock:
		if not _isTerminating and _player is None:
			_player = _newPlayer(sampleRate)


def _handleAudio(generation, payload):
	feed = _feeds.get(generation)
	if feed is None or feed.aborted or len(payload) < _AUDIO_META.size:
		return
	sampleRate, markerCount = _AUDIO_META.unpack_from(payload)
	markerBytes = markerCount * _AUDIO_MARKER.size
	dataOffset = _AUDIO_META.size + markerBytes
	if dataOffset > len(payload):
		log.error("Malformed eSpeak/MBROLA audio packet")
		return
	markers = []
	for i in range(markerCount):
		markers.append(_AUDIO_MARKER.unpack_from(payload, _AUDIO_META.size + i * _AUDIO_MARKER.size))
	pcm = payload[dataOffset:]
	_ensurePlayerRate(sampleRate)
	player = _player
	if player is None:
		return
	pending = _pendingIndexes.pop(generation, [])
	offset = 0
	for index, byteOffset in markers:
		byteOffset = min(len(pcm), max(offset, byteOffset)) & ~1
		pending.append(index)
		if byteOffset > offset:
			# Feed the audio in front of this mark; its indexes are announced
			# once that audio has played, so a mark never runs ahead of sound.
			if not _feedSegment(feed, player, pcm[offset:byteOffset], pending):
				return
			pending = []
			offset = byteOffset
	if offset < len(pcm):
		if not _feedSegment(feed, player, pcm[offset:], pending):
			return
	elif pending:
		# No audio left in this packet: carry the marks to the next one.
		_pendingIndexes[generation] = pending


def _feedSegment(feed, player, segment, indexes):
	"""Hand one chunk of audio to the player.

	@return C{False} if the utterance was cancelled; its audio must not play.
	"""
	if feed.aborted:
		return False
	# Count the chunk before feeding: a very short chunk can finish playing, and
	# thus call back, before feed() returns.
	feed.pending += 1
	player.feed(segment, onDone=lambda: _segmentDone(feed, indexes))
	if feed.aborted:
		# stop() ran while feed() was blocked on a full player buffer. Anything
		# this call managed to queue arrived after the player was stopped, so it
		# has to be discarded or the interrupted utterance would resume midway.
		feed.pending = max(0, feed.pending - 1)
		player.stop()
		return False
	return True


def _fireSwallowedIndexes(feed, upTo=None):
	"""Announce marks eSpeak dropped, keeping the rest in order.

	eSpeak NG loses a ``<mark>`` that sits on a sentence boundary. NVDA needs
	every index it sent and expects them in order, so a swallowed mark is
	announced just before the next mark that is reported, or at the end of the
	utterance for a trailing one. C{upTo} is left to the caller to announce.
	"""
	for index in feed.expected:
		if index == upTo:
			return
		if index in feed.seen:
			continue
		feed.seen.add(index)
		if _onIndexReached:
			_onIndexReached(index)


def _segmentDone(feed, indexes):
	feed.pending = max(0, feed.pending - 1)
	if feed.aborted or feed.doneNotified:
		return
	for index in indexes:
		if feed.aborted:
			return
		_fireSwallowedIndexes(feed, upTo=index)
		if index in feed.seen:
			continue
		feed.seen.add(index)
		if _onIndexReached:
			_onIndexReached(index)
	if feed.complete and feed.pending <= 0:
		_notifyDoneSpeaking(feed)


def _notifyDoneSpeaking(feed):
	if feed.aborted or feed.doneNotified:
		return
	feed.doneNotified = True
	_pendingIndexes.pop(feed.generation, None)
	_fireSwallowedIndexes(feed)
	if _onIndexReached:
		_onIndexReached(None)


def _notifyDone(generation, flush=False):
	feed = _feeds.get(generation)
	if feed is None or feed.aborted:
		return
	feed.complete = True
	if flush and feed.pending > 0 and generation == _generation:
		# nvwave's WASAPI player only dispatches chunk completion callbacks from
		# feed() or sync()/idle(); its playback thread never calls them itself.
		# Without this final flush the last chunk's callback never runs, so the
		# index attached to the end of the utterance (and synthDoneSpeaking)
		# would never reach NVDA. NVDA's own eSpeak driver calls player.idle()
		# at the end of an utterance for the same reason.
		player = _player
		if player is not None:
			try:
				player.idle()
			except Exception:
				log.debugWarning("Unable to flush eSpeak/MBROLA audio", exc_info=True)
	if feed.aborted:
		return
	if feed.pending <= 0:
		_notifyDoneSpeaking(feed)
	else:
		# The player dropped some completion callbacks (for example because it
		# was stopped). Never leave NVDA waiting for the end of an utterance.
		feed.pending = 0
		_notifyDoneSpeaking(feed)


def initialize(indexCallback=None):
	global _host, _onIndexReached, _voices, _currentVoice, _parameters
	global _player, _playerSampleRate, _isTerminating
	terminate()
	_onIndexReached = indexCallback
	_isTerminating = False
	with _stateLock:
		_feeds.clear()
		_pendingIndexes.clear()
	_host = _Host()
	ready = _host.start()
	_voices = []
	for item in ready.get("voices", []):
		identifier = str(item.get("id", ""))
		if not identifier:
			continue
		language = str(item.get("language", ""))
		_voices.append(_Voice(
			str(item.get("name", identifier)).encode("utf-8"),
			("5" + language).encode("utf-8"),
			identifier.encode("utf-8"),
		))
	_parameters = {int(key): int(value) for key, value in ready.get("parameters", {}).items()}
	# Do not open a player at the host's default rate: the first audio packet
	# carries the voice's real rate, and swapping players mid-utterance can cut
	# the start of speech. The player is created on demand by _ensurePlayerRate.
	_playerSampleRate = 0
	_player = None


def info():
	return _host.readyData.get("info", "eSpeak with MBROLA") if _host else "eSpeak with MBROLA"


def getEngineDirectory():
	"""Return the folder containing the engine DLLs and eSpeak data."""
	return os.environ.get("ESPEAK_MB_ENGINE_DIR") or os.path.dirname(os.path.abspath(__file__))


def getMbrolaVoiceList():
	"""Return only MBROLA voices whose database and translation table exist."""
	engineDir = getEngineDirectory()
	dataDir = os.path.join(engineDir, "espeak-ng-data")
	result = []
	for voice in _voices:
		identifier = os.path.basename(voice.identifier.decode("utf-8"))
		if not identifier.startswith("mb-"):
			continue
		descriptor = os.path.join(dataDir, "voices", "mb", identifier)
		database = identifier[3:]
		phtrans = None
		try:
			with open(descriptor, "r", encoding="utf-8", errors="replace") as source:
				for line in source:
					fields = line.split()
					if len(fields) >= 3 and fields[0].lower() == "mbrola":
						database = fields[1]
						phtrans = fields[2]
						break
		except OSError:
			continue
		if not phtrans:
			continue
		if not (
			os.path.isfile(os.path.join(dataDir, "mbrola", database))
			or os.path.isfile(os.path.join(dataDir, "mbrola", database, database))
		):
			continue
		if not os.path.isfile(os.path.join(dataDir, "mbrola_ph", phtrans)):
			continue
		result.append(voice)
	return result


def getVoiceList():
	return list(_voices)


def getCurrentVoice():
	return _currentVoice


def setVoiceByLanguage(language):
	global _currentVoice
	language = str(language or "en").replace("_", "-")
	voiceId = _host.setLanguage(language)
	_currentVoice = next((voice for voice in _voices if voice.identifier.decode("utf-8") == voiceId), None)
	if _currentVoice is None:
		_currentVoice = next((voice for voice in _voices if voice.identifier.decode("utf-8") == "mb-en1"), None)


def setVoiceByName(name):
	global _currentVoice
	voiceId = _host.setVoice(decodeEspeakString(name))
	_currentVoice = next((voice for voice in _voices if voice.identifier.decode("utf-8") == voiceId), None)
	if _currentVoice is None:
		_currentVoice = next((voice for voice in _voices if voice.identifier.decode("utf-8") == decodeEspeakString(name)), None)


def setVoice(voice):
	setVoiceByName(voice.identifier)


def _setVoiceAndVariant(voice=None, variant=None):
	current = getCurrentVoice()
	if not voice:
		voice = decodeEspeakString(current.identifier) if current else "en"
	if not variant:
		variant = "none"
	variant = str(variant).lower()
	setVoiceByName(voice if variant == "none" else "%s+%s" % (voice, variant))


def setVoiceAndVariant(voice=None, variant=None):
	_setVoiceAndVariant(voice, variant)


def setParameter(parameter, value, relative):
	value = int(value)
	if relative:
		value += _parameters.get(int(parameter), 0)
	_parameters[int(parameter)] = value
	if _host:
		_host.setParameter(parameter, value)


def getParameter(parameter, current=1):
	return int(_parameters.get(int(parameter), 0))


def speak(text, expectedIndexes=None):
	global _generation
	if not _host:
		raise _HostError("eSpeak/MBROLA host is not initialized")
	with _stateLock:
		_generation += 1
		generation = _generation
		# A new utterance does not cancel the previous one: NVDA decides that by
		# calling stop() first. Keeping the old feed alive lets queued speech play
		# back to back without gaps.
		feed = _Feed(generation, expectedIndexes or ())
		_feeds[generation] = feed
		for stale in [key for key in _feeds if key <= generation - 8]:
			_feeds[stale].aborted = True
			del _feeds[stale]
			_pendingIndexes.pop(stale, None)
	try:
		_host.speak(generation, text)
	except Exception:
		log.error("Unable to send speech to eSpeak/MBROLA host", exc_info=True)
		_notifyDone(generation)


def stop():
	global _generation
	with _stateLock:
		oldGeneration = _generation
		_generation += 1
		# Everything handed to the host so far belongs to an interrupted
		# utterance: its remaining audio and its index marks must be dropped.
		for feed in _feeds.values():
			feed.aborted = True
		_pendingIndexes.clear()
		player = _player
	if _host:
		_host.cancel(oldGeneration)
	if player:
		player.stop()


def pause(switch):
	if _player:
		_player.pause(switch)


def terminate():
	global _host, _player, _playerSampleRate, _isTerminating, _generation
	_isTerminating = True
	with _stateLock:
		player = _player
		_player = None
		_playerSampleRate = 0
		_generation += 1
		for feed in _feeds.values():
			feed.aborted = True
		_feeds.clear()
		_pendingIndexes.clear()
	if _host:
		_host.stop()
		_host = None
	if player:
		try:
			player.stop()
			player.close()
		except Exception:
			log.debugWarning("Unable to close eSpeak/MBROLA WavePlayer", exc_info=True)
