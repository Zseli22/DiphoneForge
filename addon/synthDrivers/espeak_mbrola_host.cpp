// 32-bit eSpeak/MBROLA host for the NVDA 2026 x64 driver bridge.
// Build with: g++ -m32 -std=c++17 -O2 -Wall -Wextra -o espeakMbrolaHost.exe espeak_mbrola_host.cpp

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <fcntl.h>
#include <io.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <limits>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr uint32_t MAGIC = 0x484B544D; // "MTKH" on the wire
constexpr uint32_t READY = 1;
constexpr uint32_t AUDIO = 2;
constexpr uint32_t DONE = 3;
constexpr uint32_t CANCELLED = 4;
constexpr uint32_t ERROR_MESSAGE = 5;
constexpr uint32_t SAMPLE_RATE = 6;
constexpr uint32_t RESPONSE = 7;

constexpr uint32_t SET_PARAMETER = 10;
constexpr uint32_t SET_VOICE = 11;
constexpr uint32_t SET_LANGUAGE = 12;
constexpr uint32_t SPEAK = 13;
constexpr uint32_t CANCEL = 14;
constexpr uint32_t QUIT = 0xFFFF;

constexpr int AUDIO_OUTPUT_SYNCHRONOUS = 2;
constexpr int ESPEAK_INITIALIZE_DONT_EXIT = 0x8000;
constexpr unsigned int ESPEAK_CHARS_UTF8 = 1;
constexpr unsigned int ESPEAK_SSML = 0x10;
constexpr unsigned int ESPEAK_PHONEMES = 0x100;
constexpr int ESPEAK_EVENT_LIST_TERMINATED = 0;
constexpr int ESPEAK_EVENT_MARK = 3;
constexpr int ESPEAK_EVENT_SAMPLE_RATE = 8;
constexpr size_t MAX_PACKET_SIZE = 16 * 1024 * 1024;

#pragma pack(push, 1)
struct Header {
	uint32_t magic;
	uint32_t type;
	uint32_t generation;
	uint32_t size;
};

struct AudioMeta {
	uint32_t sampleRate;
	uint32_t markerCount;
};

struct AudioMarker {
	int32_t index;
	uint32_t byteOffset;
};

struct ParameterData {
	int32_t parameter;
	int32_t value;
};
#pragma pack(pop)

union EspeakEventId {
	int number;
	const char* name;
	char string[8];
};

struct EspeakEvent {
	int type;
	unsigned int uniqueIdentifier;
	int textPosition;
	int length;
	int audioPosition;
	int sample;
	void* userData;
	EspeakEventId id;
};

struct EspeakVoice {
	const char* name;
	const char* languages;
	const char* identifier;
	int8_t gender;
	int8_t age;
	int8_t variant;
	int8_t spareByte;
	int score;
	void* spare;
};

using SynthCallback = int(__cdecl*)(short*, int, EspeakEvent*);
using Initialize = int(__cdecl*)(int, int, const char*, int);
using SetSynthCallback = void(__cdecl*)(SynthCallback);
using Synth = int(__cdecl*)(const void*, size_t, unsigned int, int, unsigned int, unsigned int, unsigned int*, void*);
using SetVoiceByName = int(__cdecl*)(const char*);
using SetVoiceByProperties = int(__cdecl*)(EspeakVoice*);
using SetParameter = int(__cdecl*)(int, int, int);
using GetParameter = int(__cdecl*)(int, int);
using ListVoices = const EspeakVoice**(__cdecl*)(const EspeakVoice*);
using GetCurrentVoice = const EspeakVoice*(__cdecl*)();
using GetInfo = const char*(__cdecl*)(const char**);
using Terminate = int(__cdecl*)();
using SetPhonemeTrace = void(__cdecl*)(int, FILE*);

using UcrtFopen = FILE*(__cdecl*)(const char*, const char*);
using UcrtFclose = int(__cdecl*)(FILE*);

// The MBROLA DLL exports the standard mbrowrap API. These entry points let the
// host synthesize straight into memory instead of spawning mbrola.exe and
// writing a temporary .wav file.
using MbrolaInit = int(__stdcall*)(char*);
using MbrolaClose = void(__stdcall*)();
using MbrolaRead = int(__stdcall*)(short*, int);
using MbrolaWrite = int(__stdcall*)(char*);
using MbrolaFlush = int(__stdcall*)();
using MbrolaGetFreq = int(__stdcall*)();
using MbrolaSetNoError = void(__stdcall*)(int);
using MbrolaLastError = char*(__stdcall*)(char*, int);

HANDLE inputHandle = INVALID_HANDLE_VALUE;
HANDLE outputHandle = INVALID_HANDLE_VALUE;
std::mutex outputMutex;
std::mutex engineMutex;
std::mutex parameterMutex;
std::atomic<bool> cancelled(false);
std::atomic<uint32_t> activeGeneration(0);
std::atomic<int> currentSampleRate(22050);
uint64_t utteranceBytesSent = 0; // Accessed only by the eSpeak synthesis thread.
std::map<int, int> pendingParameters;
std::string engineDirectory;

// eSpeak's .pho output goes through the UCRT that the DLL uses, not the
// MinGW runtime's FILE*.
HMODULE ucrtModule = nullptr;
UcrtFopen ucrtFopen = nullptr;
UcrtFclose ucrtFclose = nullptr;

struct ProbeMarker {
	int index;
	int timeMs;
};
std::vector<ProbeMarker> probeMarkers;
bool collectProbeMarkers = false;

constexpr int MBROLA_BASE_RATE = 175;
constexpr int PHO_VOWEL_FLOOR_MS = 25;
constexpr int PHO_CONSONANT_FLOOR_MS = 15;
constexpr int PHO_PAUSE_FLOOR_MS = 8;
// Volume 100 is 2.4x the database output. The database itself peaks about 6 dB
// below full scale on running speech, so the extra gain is what makes the voice
// sit at the same loudness as a normal synthesizer. ESPEAK_MB_GAIN overrides the
// value without a rebuild, which makes it easy to compare levels by ear.
constexpr double PHO_VOLUME_GAIN = 2.4;
constexpr double PHO_LIMIT_KNEE = 0.70;
constexpr double PHO_LIMIT_CEILING = 0.97;

double phoVolumeGain() {
	static const double gain = [] {
		const char* override = std::getenv("ESPEAK_MB_GAIN");
		if (override != nullptr) {
			char* end = nullptr;
			const double parsed = std::strtod(override, &end);
			if (end != override && parsed > 0.0) return parsed;
		}
		return PHO_VOLUME_GAIN;
	}();
	return gain;
}

// Keep the extra gain from turning loud vowels into hard clipping. The curve is
// linear up to the knee and bends smoothly to the ceiling, so it touches only
// the few percent of samples that would otherwise hit full scale. It is
// stateless, so the 20 ms streaming packets stay sample-for-sample aligned with
// the index marks.
int16_t limitSample(double value) {
	constexpr double FULL_SCALE = 32767.0;
	const double normalized = value / FULL_SCALE;
	const double magnitude = std::fabs(normalized);
	if (magnitude <= PHO_LIMIT_KNEE) {
		return static_cast<int16_t>(std::lround(value));
	}
	const double excess = (magnitude - PHO_LIMIT_KNEE) / (PHO_LIMIT_CEILING - PHO_LIMIT_KNEE);
	const double shaped = PHO_LIMIT_KNEE
		+ (PHO_LIMIT_CEILING - PHO_LIMIT_KNEE) * std::tanh(excess);
	return static_cast<int16_t>(std::lround(std::copysign(shaped, normalized) * FULL_SCALE));
}

void applyOutputGain(std::vector<int16_t>& samples, double gain) {
	if (gain == 1.0) return;
	for (auto& sample : samples) {
		sample = limitSample(sample * gain);
	}
}

bool traceEvents() {
	static const bool enabled = std::getenv("ESPEAK_MB_TRACE_EVENTS") != nullptr;
	return enabled;
}

HMODULE espeakModule = nullptr;
HMODULE mbrolaModule = nullptr;
Initialize espeakInitialize = nullptr;
SetSynthCallback espeakSetSynthCallback = nullptr;
Synth espeakSynth = nullptr;
SetVoiceByName espeakSetVoiceByName = nullptr;
SetVoiceByProperties espeakSetVoiceByProperties = nullptr;
SetParameter espeakSetParameter = nullptr;
GetParameter espeakGetParameter = nullptr;
ListVoices espeakListVoices = nullptr;
GetCurrentVoice espeakGetCurrentVoice = nullptr;
GetInfo espeakInfo = nullptr;
Terminate espeakTerminate = nullptr;
SetPhonemeTrace espeakSetPhonemeTrace = nullptr;

// MBROLA entry points, when the streaming mbrola_stream.dll is available. It is
// built from the same source as mbrola.exe, so its output matches the accepted
// mbrola.exe pipeline, but it can synthesise straight into memory.
MbrolaInit mbrolaInit = nullptr;
MbrolaClose mbrolaClose = nullptr;
MbrolaRead mbrolaRead = nullptr;
MbrolaWrite mbrolaWrite = nullptr;
MbrolaFlush mbrolaFlush = nullptr;
MbrolaGetFreq mbrolaGetFreq = nullptr;
MbrolaSetNoError mbrolaSetNoError = nullptr;
MbrolaLastError mbrolaLastError = nullptr;

// State for the in-process MBROLA engine. Only the synthesis thread touches it,
// and a new utterance joins the previous one before starting.
bool mbrolaEngineOpen = false;

template <typename T>
T loadSymbol(HMODULE module, const char* symbol) {
	return reinterpret_cast<T>(GetProcAddress(module, symbol));
}

bool readExact(void* buffer, uint32_t size) {
	auto* cursor = static_cast<uint8_t*>(buffer);
	while (size) {
		DWORD count = 0;
		if (!ReadFile(inputHandle, cursor, size, &count, nullptr) || count == 0) return false;
		cursor += count;
		size -= count;
	}
	return true;
}

bool writeExactLocked(const void* buffer, uint32_t size) {
	const auto* cursor = static_cast<const uint8_t*>(buffer);
	while (size) {
		DWORD count = 0;
		if (!WriteFile(outputHandle, cursor, size, &count, nullptr) || count == 0) return false;
		cursor += count;
		size -= count;
	}
	return true;
}

bool sendMessage(uint32_t type, uint32_t generation, const void* data, uint32_t size) {
	if (size > MAX_PACKET_SIZE) return false;
	const Header header{MAGIC, type, generation, size};
	std::lock_guard<std::mutex> lock(outputMutex);
	return writeExactLocked(&header, sizeof(header)) && (!size || writeExactLocked(data, size));
}

bool sendText(uint32_t type, uint32_t generation, const std::string& text) {
	return sendMessage(type, generation, text.data(), static_cast<uint32_t>(text.size()));
}

std::string jsonEscape(const char* value) {
	std::ostringstream out;
	for (const unsigned char* p = reinterpret_cast<const unsigned char*>(value ? value : ""); *p; ++p) {
		switch (*p) {
		case '"': out << "\\\""; break;
		case '\\': out << "\\\\"; break;
	case '\b': out << "\\b"; break;
	case '\f': out << "\\f"; break;
	case '\n': out << "\\n"; break;
	case '\r': out << "\\r"; break;
	case '\t': out << "\\t"; break;
		default:
			if (*p < 0x20) {
				static const char hex[] = "0123456789abcdef";
				out << "\\u00" << hex[*p >> 4] << hex[*p & 0x0f];
			} else {
				out << static_cast<char>(*p);
			}
		}
	}
	return out.str();
}

std::string basename(const char* value) {
	std::string result = value ? value : "";
	size_t slash = result.find_last_of("/\\");
	if (slash != std::string::npos) result.erase(0, slash + 1);
	return result;
}

std::string buildReadyJson(int initialRate) {
	std::ostringstream out;
	out << "{\"sampleRate\":" << initialRate << ",\"info\":\""
		<< jsonEscape(espeakInfo ? espeakInfo(nullptr) : "eSpeak") << "\",\"parameters\":{";
	for (int parameter = 1; parameter <= 9; ++parameter) {
		if (parameter != 1) out << ',';
		out << '\"' << parameter << "\":" << espeakGetParameter(parameter, 1);
	}
	out << "},\"voices\":[";
	const EspeakVoice** voices = espeakListVoices(nullptr);
	bool first = true;
	if (voices) {
		for (size_t i = 0; voices[i]; ++i) {
			const EspeakVoice* voice = voices[i];
			if (!voice->identifier || !voice->name) continue;
			if (!first) out << ',';
			first = false;
			const char* language = voice->languages && voice->languages[0] ? voice->languages + 1 : "";
			out << "{\"id\":\"" << jsonEscape(basename(voice->identifier).c_str())
				<< "\",\"name\":\"" << jsonEscape(voice->name)
				<< "\",\"language\":\"" << jsonEscape(language) << "\"}";
		}
	}
	out << "]}";
	return out.str();
}

bool initializeEngine(const char* enginePath, std::string& error) {
	engineDirectory = enginePath;
	if (!SetDllDirectoryA(enginePath)) {
		error = "SetDllDirectory failed";
		return false;
	}
	mbrolaModule = LoadLibraryA((std::string(enginePath) + "\\mbrola.dll").c_str());
	if (!mbrolaModule) {
		error = "Unable to load mbrola.dll (Win32 error " + std::to_string(GetLastError()) + ")";
		return false;
	}
	espeakModule = LoadLibraryA((std::string(enginePath) + "\\espeak.dll").c_str());
	if (!espeakModule) {
		error = "Unable to load espeak.dll (Win32 error " + std::to_string(GetLastError()) + ")";
		return false;
	}
	ucrtModule = LoadLibraryA("ucrtbase.dll");
	if (ucrtModule) {
		ucrtFopen = loadSymbol<UcrtFopen>(ucrtModule, "fopen");
		ucrtFclose = loadSymbol<UcrtFclose>(ucrtModule, "fclose");
	}

	espeakInitialize = loadSymbol<Initialize>(espeakModule, "espeak_Initialize");
	espeakSetSynthCallback = loadSymbol<SetSynthCallback>(espeakModule, "espeak_SetSynthCallback");
	espeakSynth = loadSymbol<Synth>(espeakModule, "espeak_Synth");
	espeakSetVoiceByName = loadSymbol<SetVoiceByName>(espeakModule, "espeak_SetVoiceByName");
	espeakSetVoiceByProperties = loadSymbol<SetVoiceByProperties>(espeakModule, "espeak_SetVoiceByProperties");
	espeakSetParameter = loadSymbol<SetParameter>(espeakModule, "espeak_SetParameter");
	espeakGetParameter = loadSymbol<GetParameter>(espeakModule, "espeak_GetParameter");
	espeakListVoices = loadSymbol<ListVoices>(espeakModule, "espeak_ListVoices");
	espeakGetCurrentVoice = loadSymbol<GetCurrentVoice>(espeakModule, "espeak_GetCurrentVoice");
	espeakInfo = loadSymbol<GetInfo>(espeakModule, "espeak_Info");
	espeakTerminate = loadSymbol<Terminate>(espeakModule, "espeak_Terminate");
	espeakSetPhonemeTrace = loadSymbol<SetPhonemeTrace>(espeakModule, "espeak_SetPhonemeTrace");
	if (!espeakInitialize || !espeakSetSynthCallback || !espeakSynth || !espeakSetVoiceByName
		|| !espeakSetVoiceByProperties || !espeakSetParameter || !espeakGetParameter
		|| !espeakListVoices || !espeakGetCurrentVoice || !espeakInfo || !espeakTerminate
		|| !espeakSetPhonemeTrace || !ucrtFopen || !ucrtFclose) {
		error = "The bundled espeak.dll is missing a required eSpeak API export";
		return false;
	}

	// The in-process MBROLA engine is optional: if mbrola_stream.dll is missing
	// or lacks an entry point, utterances fall back to the mbrola.exe path.
	//
	// mbrola.dll belongs to eSpeak and must not be used here: sharing it would
	// couple the two engines' state. mbrola_stream.dll is a separate module with
	// its own state, built from the same sources as mbrola.exe.
	HMODULE streamModule = LoadLibraryA((std::string(enginePath) + "\\mbrola_stream.dll").c_str());
	if (streamModule) {
		mbrolaInit = loadSymbol<MbrolaInit>(streamModule, "init_MBR");
		mbrolaClose = loadSymbol<MbrolaClose>(streamModule, "close_MBR");
		mbrolaRead = loadSymbol<MbrolaRead>(streamModule, "read_MBR");
		mbrolaWrite = loadSymbol<MbrolaWrite>(streamModule, "write_MBR");
		mbrolaFlush = loadSymbol<MbrolaFlush>(streamModule, "flush_MBR");
		mbrolaGetFreq = loadSymbol<MbrolaGetFreq>(streamModule, "getFreq_MBR");
		mbrolaSetNoError = loadSymbol<MbrolaSetNoError>(streamModule, "setNoError_MBR");
		mbrolaLastError = loadSymbol<MbrolaLastError>(streamModule, "lastErrorStr_MBR");
	}
	if (!mbrolaInit || !mbrolaClose || !mbrolaRead || !mbrolaWrite
		|| !mbrolaFlush || !mbrolaGetFreq) {
		mbrolaInit = nullptr;
		mbrolaClose = nullptr;
		mbrolaRead = nullptr;
		mbrolaWrite = nullptr;
		mbrolaFlush = nullptr;
		mbrolaGetFreq = nullptr;
	}

	const int sampleRate = espeakInitialize(
		AUDIO_OUTPUT_SYNCHRONOUS,
		300,
		enginePath,
		ESPEAK_INITIALIZE_DONT_EXIT
	);
	if (sampleRate <= 0) {
		error = "espeak_Initialize failed with code " + std::to_string(sampleRate);
		return false;
	}
	currentSampleRate.store(sampleRate);
	return true;
}

int __cdecl synthesisCallback(short* wav, int numSamples, EspeakEvent* events);

int __cdecl synthesisCallback(short* wav, int numSamples, EspeakEvent* events) {
	const uint32_t generation = activeGeneration.load();
	if (cancelled.load()) return 1;

	std::vector<AudioMarker> markers;
	const EspeakEvent* event = events;
	if (event) {
		for (; event->type != ESPEAK_EVENT_LIST_TERMINATED; ++event) {
			if (traceEvents()) {
				std::fprintf(stderr, "eSpeak event: type=%d audioPosition=%d", event->type, event->audioPosition);
				if (event->type == ESPEAK_EVENT_MARK && event->id.name) {
					std::fprintf(stderr, " mark=%s", event->id.name);
				}
				if (event->type == ESPEAK_EVENT_SAMPLE_RATE) {
					std::fprintf(stderr, " sampleRate=%d", event->id.number);
				}
				std::fputc('\n', stderr);
			}
			if (event->type == ESPEAK_EVENT_SAMPLE_RATE && event->id.number > 0) {
				currentSampleRate.store(event->id.number);
				const int32_t rate = event->id.number;
				sendMessage(SAMPLE_RATE, generation, &rate, sizeof(rate));
			} else if (event->type == ESPEAK_EVENT_MARK && event->id.name) {
				char* end = nullptr;
				const long index = std::strtol(event->id.name, &end, 10);
				if (end != event->id.name) {
					const uint64_t absolute = static_cast<uint64_t>(std::max(0, event->audioPosition))
						* static_cast<uint64_t>(currentSampleRate.load()) * sizeof(int16_t) / 1000;
					const uint64_t relative = absolute > utteranceBytesSent ? absolute - utteranceBytesSent : 0;
					markers.push_back({static_cast<int32_t>(index), static_cast<uint32_t>(relative)});
				}
			}
		}
	}

	if (!wav || numSamples <= 0) return 0;
	if (cancelled.load()) return 1;

	const uint32_t audioBytes = static_cast<uint32_t>(numSamples) * sizeof(int16_t);
	for (auto& marker : markers) marker.byteOffset = std::min(marker.byteOffset, audioBytes);
	AudioMeta meta{static_cast<uint32_t>(currentSampleRate.load()), static_cast<uint32_t>(markers.size())};
	std::vector<uint8_t> payload(sizeof(meta) + markers.size() * sizeof(AudioMarker) + audioBytes);
	std::memcpy(payload.data(), &meta, sizeof(meta));
	if (!markers.empty()) std::memcpy(payload.data() + sizeof(meta), markers.data(), markers.size() * sizeof(AudioMarker));
	std::memcpy(payload.data() + sizeof(meta) + markers.size() * sizeof(AudioMarker), wav, audioBytes);
	if (!sendMessage(AUDIO, generation, payload.data(), static_cast<uint32_t>(payload.size()))) return 1;
	utteranceBytesSent += audioBytes;
	return cancelled.load() ? 1 : 0;
}

int __cdecl probeCallback(short* wav, int numSamples, EspeakEvent* events) {
	(void)wav;
	(void)numSamples;
	if (collectProbeMarkers && events) {
		for (const EspeakEvent* event = events; event->type != ESPEAK_EVENT_LIST_TERMINATED; ++event) {
			if (event->type != ESPEAK_EVENT_MARK || !event->id.name) continue;
			char* end = nullptr;
			const long index = std::strtol(event->id.name, &end, 10);
			if (end != event->id.name) probeMarkers.push_back({static_cast<int>(index), event->audioPosition});
		}
	}
	return cancelled.load() ? 1 : 0;
}

bool readTextFile(const std::string& path, std::string& text) {
	std::ifstream file(path, std::ios::binary);
	if (!file) return false;
	std::ostringstream buffer;
	buffer << file.rdbuf();
	text = buffer.str();
	return true;
}

std::string makeTempPath(const char* suffix) {
	char tempPath[MAX_PATH] = {};
	char tempName[MAX_PATH] = {};
	if (!GetTempPathA(MAX_PATH, tempPath)) return {};
	if (!GetTempFileNameA(tempPath, "mbr", 0, tempName)) return {};
	DeleteFileA(tempName);
	return std::string(tempName) + suffix;
}

bool isVowelPhone(const std::string& phone) {
	static const std::set<std::string> vowels = {
		"a", "A", "A:", "E", "I", "O", "U", "@", "3", "3:", "i:", "u:",
		"aI", "aU", "eI", "OI", "oU", "0", "0:",
	};
	return vowels.count(phone) != 0;
}

struct PhoLine {
	std::string phone;
	int originalMs;
	int adjustedMs;
	std::string tail;
};

bool adjustPho(
	const std::string& input,
	int targetRate,
	std::vector<PhoLine>& lines,
	std::vector<int>& originalCumulative,
	std::vector<int>& adjustedCumulative,
	std::string& adjustedText
) {
	std::ifstream source(input);
	if (!source) return false;
	const double factor = static_cast<double>(MBROLA_BASE_RATE) / std::max(1, targetRate);
	std::string line;
	lines.clear();
	while (std::getline(source, line)) {
		if (line.empty()) continue;
		const size_t firstTab = line.find('\t');
		if (firstTab == std::string::npos) continue;
		const size_t secondTab = line.find('\t', firstTab + 1);
		const std::string phone = line.substr(0, firstTab);
		const std::string durationText = secondTab == std::string::npos
			? line.substr(firstTab + 1)
			: line.substr(firstTab + 1, secondTab - firstTab - 1);
		const std::string tail = secondTab == std::string::npos ? "" : line.substr(secondTab + 1);
		int duration = 0;
		try {
			duration = std::stoi(durationText);
		} catch (...) {
			continue;
		}
		const int floorMs = phone == "_"
			? PHO_PAUSE_FLOOR_MS
			: (isVowelPhone(phone) ? PHO_VOWEL_FLOOR_MS : PHO_CONSONANT_FLOOR_MS);
		const int rounded = static_cast<int>(std::nearbyint(duration * factor));
		const int adjusted = std::max(floorMs, rounded);
		lines.push_back({phone, duration, adjusted, tail});
	}
	if (lines.empty()) return false;
	originalCumulative.assign(lines.size() + 1, 0);
	adjustedCumulative.assign(lines.size() + 1, 0);
	for (size_t i = 0; i < lines.size(); ++i) {
		originalCumulative[i + 1] = originalCumulative[i] + lines[i].originalMs;
		adjustedCumulative[i + 1] = adjustedCumulative[i] + lines[i].adjustedMs;
	}
	adjustedText.clear();
	for (const auto& item : lines) {
		adjustedText += item.phone;
		adjustedText += '\t';
		adjustedText += std::to_string(item.adjustedMs);
		if (!item.tail.empty()) {
			adjustedText += '\t';
			adjustedText += item.tail;
		}
		adjustedText += '\n';
	}
	return true;
}

double mapMarkerTime(
	int timeMs,
	const std::vector<PhoLine>& lines,
	const std::vector<int>& originalCumulative,
	const std::vector<int>& adjustedCumulative
) {
	if (timeMs <= 0) return 0;
	for (size_t i = 0; i < lines.size(); ++i) {
		if (timeMs <= originalCumulative[i + 1]) {
			const int original = lines[i].originalMs;
			double fraction = original > 0 ? static_cast<double>(timeMs - originalCumulative[i]) / original : 0.0;
			fraction = std::clamp(fraction, 0.0, 1.0);
			return adjustedCumulative[i] + fraction * lines[i].adjustedMs;
		}
	}
	return adjustedCumulative.empty() ? 0.0 : adjustedCumulative.back();
}

bool resolveMbrolaPaths(std::string& executable, std::string& database) {
	const EspeakVoice* voice = espeakGetCurrentVoice ? espeakGetCurrentVoice() : nullptr;
	if (!voice || !voice->identifier) return false;
	const std::string identifier = basename(voice->identifier);
	std::string descriptor = engineDirectory + "\\espeak-ng-data\\voices\\mb\\" + identifier;
	std::string text;
	if (!readTextFile(descriptor, text)) {
		descriptor = engineDirectory + "\\espeak-ng-data\\voices\\" + identifier;
		if (!readTextFile(descriptor, text)) return false;
	}
	std::istringstream source(text);
	std::string line;
	std::string databaseName;
	while (std::getline(source, line)) {
		std::istringstream fields(line);
		std::string key;
		std::string value;
		if (fields >> key >> value && key == "mbrola") {
			databaseName = value;
			break;
		}
	}
	if (databaseName.empty()) return false;
	const std::string base = engineDirectory + "\\espeak-ng-data\\mbrola\\";
	std::string candidate = base + databaseName;
	DWORD attributes = GetFileAttributesA(candidate.c_str());
	if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) {
		candidate = base + databaseName + "\\" + databaseName;
		attributes = GetFileAttributesA(candidate.c_str());
		if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) return false;
	}
	database = candidate;
	executable = engineDirectory + "\\mbrola.exe";
	attributes = GetFileAttributesA(executable.c_str());
	if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) executable.clear();
	return true;
}

// ---------------------------------------------------------------------------
// In-process MBROLA synthesis.
//
// Calling into mbrola.dll directly avoids spawning mbrola.exe and writing a
// temporary .wav for every utterance, and lets audio be pushed to NVDA while
// the rest of the utterance is still being synthesised. That keeps
// back-to-back utterances gapless and makes interruption cheap.

bool mbrolaDllAvailable() {
	return mbrolaInit && mbrolaClose && mbrolaRead
		&& mbrolaWrite && mbrolaFlush && mbrolaGetFreq;
}

void closeMbrolaEngine() {
	if (mbrolaEngineOpen && mbrolaClose) mbrolaClose();
	mbrolaEngineOpen = false;
}

bool openMbrolaEngine(const std::string& database) {
	if (!mbrolaDllAvailable()) return false;
	if (mbrolaEngineOpen) {
		// Reusing an open engine changes the output slightly (MBROLA carries
		// filter and buffer state between utterances), so start from a clean
		// instance every time.
		closeMbrolaEngine();
	}
	std::string path = database;
	if (mbrolaInit(&path[0]) != 0) {
		if (mbrolaLastError) {
			char message[256] = {};
			mbrolaLastError(message, sizeof(message));
			if (message[0]) std::fprintf(stderr, "mbrola init failed: %s\n", message);
		}
		return false;
	}
	if (mbrolaSetNoError) mbrolaSetNoError(1);
	mbrolaEngineOpen = true;
	return true;
}

// The streaming engine is a private module, so it simply gets a clean instance
// for every utterance.
bool prepareMbrolaEngine(const std::string& database) {
	closeMbrolaEngine();
	if (!openMbrolaEngine(database)) return false;
	return mbrolaGetFreq() > 0;
}

bool sendAudioPacket(
	uint32_t generation,
	int sampleRate,
	const int16_t* samples,
	size_t sampleCount,
	const std::vector<AudioMarker>& markers
) {
	if (sampleRate <= 0) return false;
	const size_t audioBytes = sampleCount * sizeof(int16_t);
	AudioMeta meta{static_cast<uint32_t>(sampleRate), static_cast<uint32_t>(markers.size())};
	std::vector<uint8_t> payload(sizeof(meta) + markers.size() * sizeof(AudioMarker) + audioBytes);
	std::memcpy(payload.data(), &meta, sizeof(meta));
	if (!markers.empty()) {
		std::memcpy(payload.data() + sizeof(meta), markers.data(), markers.size() * sizeof(AudioMarker));
	}
	if (audioBytes) {
		std::memcpy(payload.data() + sizeof(meta) + markers.size() * sizeof(AudioMarker), samples, audioBytes);
	}
	return sendMessage(AUDIO, generation, payload.data(), static_cast<uint32_t>(payload.size()));
}

bool streamMbrolaAudio(
	uint32_t generation,
	const std::string& phonemes,
	int sampleRate,
	std::vector<AudioMarker> markers,
	double gain,
	size_t maxSamples
) {
	if (sampleRate <= 0 || phonemes.empty()) return false;
	std::sort(markers.begin(), markers.end(), [](const AudioMarker& left, const AudioMarker& right) {
		return left.byteOffset < right.byteOffset;
	});
	const int32_t rate = sampleRate;
	if (!sendMessage(SAMPLE_RATE, generation, &rate, sizeof(rate))) return false;

	// Push the phoneme stream. A partial write is retried; MBROLA buffers it.
	size_t written = 0;
	while (written < phonemes.size()) {
		if (cancelled.load()) return false;
		const std::string piece = phonemes.substr(written, std::min<size_t>(1024, phonemes.size() - written));
		std::string buffer = piece;
		const int result = mbrolaWrite(&buffer[0]);
		if (result < 0) return false;
		if (result == 0) {
			Sleep(1);
			continue;
		}
		written += static_cast<size_t>(result);
	}
	if (!mbrolaFlush()) return false;

	const size_t blockSamples = static_cast<size_t>(std::max(1, sampleRate / 50)); // 20 ms
	std::vector<short> current(blockSamples);
	std::vector<short> pending;
	size_t sentBytes = 0;
	size_t markerIndex = 0;

	// Send the block held back by one read. Marks that fall in this block are
	// attached to it, so an index is announced when its audio has played. When
	// the stream ends, marks past the end join the final block.
	auto flushPending = [&](bool final) -> bool {
		if (pending.empty()) return true;
		const size_t end = sentBytes + pending.size() * sizeof(int16_t);
		std::vector<AudioMarker> packetMarkers;
		while (markerIndex < markers.size()
			&& (final || markers[markerIndex].byteOffset <= end)) {
			packetMarkers.push_back({
				markers[markerIndex].index,
				static_cast<uint32_t>(std::min<size_t>(markers[markerIndex].byteOffset, end) - sentBytes),
			});
			++markerIndex;
		}
		if (gain != 1.0) {
			applyOutputGain(pending, gain);
		}
		if (!sendAudioPacket(generation, sampleRate, pending.data(), pending.size(), packetMarkers)) return false;
		sentBytes = end;
		pending.clear();
		return true;
	};

	while (true) {
		if (cancelled.load()) return false;
		size_t want = blockSamples;
		if (maxSamples) {
			const size_t consumed = sentBytes / sizeof(int16_t) + pending.size();
			if (consumed >= maxSamples) break;
			want = std::min(want, maxSamples - consumed);
		}
		size_t got = 0;
		while (got < want) {
			if (cancelled.load()) return false;
			const int result = mbrolaRead(current.data() + got, static_cast<int>(want - got));
			if (result <= 0) break;
			got += static_cast<size_t>(result);
		}
		if (got == 0) break;
		if (!flushPending(false)) return false;
		pending.assign(current.begin(), current.begin() + got);
		if (got < want) break;
	}
	if (!flushPending(true)) return false;
	return sentBytes > 0;
}

bool runMbrola(const std::string& executable, const std::string& database, const std::string& pho, const std::string& wav) {	std::string command = "\"" + executable + "\" \"" + database + "\" \"" + pho + "\" \"" + wav + "\"";
	std::vector<char> commandLine(command.begin(), command.end());
	commandLine.push_back('\0');
	STARTUPINFOA startup{};
	startup.cb = sizeof(startup);
	startup.dwFlags = STARTF_USESHOWWINDOW;
	startup.wShowWindow = SW_HIDE;
	PROCESS_INFORMATION process{};
	if (!CreateProcessA(
		nullptr,
		commandLine.data(),
		nullptr,
		nullptr,
		FALSE,
		CREATE_NO_WINDOW,
		nullptr,
		engineDirectory.c_str(),
		&startup,
		&process
	)) {
		return false;
	}
	bool success = false;
	while (true) {
		const DWORD wait = WaitForSingleObject(process.hProcess, 100);
		if (wait == WAIT_OBJECT_0) {
			DWORD code = 0;
			GetExitCodeProcess(process.hProcess, &code);
			success = code == 0;
			break;
		}
		if (cancelled.load()) {
			TerminateProcess(process.hProcess, 1);
			WaitForSingleObject(process.hProcess, 500);
			break;
		}
	}
	CloseHandle(process.hThread);
	CloseHandle(process.hProcess);
	return success;
}

bool readWav(const std::string& path, int& sampleRate, std::vector<int16_t>& pcm, int volume) {
	std::ifstream file(path, std::ios::binary);
	if (!file) return false;
	std::vector<uint8_t> data(
		(std::istreambuf_iterator<char>(file)),
		std::istreambuf_iterator<char>()
	);
	if (data.size() < 44 || std::memcmp(data.data(), "RIFF", 4) != 0 || std::memcmp(data.data() + 8, "WAVE", 4) != 0) {
		return false;
	}
	auto read16 = [&](size_t offset) {
		return static_cast<uint16_t>(data[offset] | (data[offset + 1] << 8));
	};
	auto read32 = [&](size_t offset) {
		return static_cast<uint32_t>(
			data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) | (data[offset + 3] << 24)
		);
	};
	size_t position = 12;
	int channels = 1;
	int bits = 16;
	size_t dataStart = 0;
	size_t dataSize = 0;
	sampleRate = 0;
	while (position + 8 <= data.size()) {
		const uint32_t size = read32(position + 4);
		position += 8;
		if (position + size > data.size()) break;
		if (std::memcmp(data.data() + position - 8, "fmt ", 4) == 0 && size >= 16) {
			channels = read16(position + 2);
			sampleRate = static_cast<int>(read32(position + 4));
			bits = read16(position + 14);
		} else if (std::memcmp(data.data() + position - 8, "data", 4) == 0) {
			dataStart = position;
			dataSize = size;
		}
		position += size + (size & 1);
	}
	if (!sampleRate || !dataStart || bits != 16 || channels <= 0) return false;
	const size_t frames = dataSize / (2 * channels);
	pcm.resize(frames);
	const int16_t* source = reinterpret_cast<const int16_t*>(data.data() + dataStart);
	for (size_t i = 0; i < frames; ++i) {
		pcm[i] = source[i * channels];
	}
	const double gain = phoVolumeGain() * std::clamp(volume, 0, 100) / 100.0;
	applyOutputGain(pcm, gain);
	return true;
}

bool sendPhoAudio(
	uint32_t generation,
	int sampleRate,
	const std::vector<int16_t>& pcm,
	const std::vector<AudioMarker>& markers
) {
	if (pcm.empty() || sampleRate <= 0) return false;
	int32_t rate = sampleRate;
	if (!sendMessage(SAMPLE_RATE, generation, &rate, sizeof(rate))) return false;
	const size_t totalBytes = pcm.size() * sizeof(int16_t);
	const size_t chunkBytes = std::max<size_t>(4096, static_cast<size_t>(sampleRate) * sizeof(int16_t));
	size_t position = 0;
	size_t markerIndex = 0;
	while (position < totalBytes) {
		const size_t end = std::min(totalBytes, position + chunkBytes);
		std::vector<AudioMarker> chunkMarkers;
		while (markerIndex < markers.size() && markers[markerIndex].byteOffset < end) {
			if (markers[markerIndex].byteOffset >= position) {
				chunkMarkers.push_back({
					markers[markerIndex].index,
					markers[markerIndex].byteOffset - static_cast<uint32_t>(position),
				});
			}
			++markerIndex;
		}
		AudioMeta meta{static_cast<uint32_t>(sampleRate), static_cast<uint32_t>(chunkMarkers.size())};
		const size_t audioBytes = end - position;
		std::vector<uint8_t> payload(sizeof(meta) + chunkMarkers.size() * sizeof(AudioMarker) + audioBytes);
		std::memcpy(payload.data(), &meta, sizeof(meta));
		if (!chunkMarkers.empty()) {
			std::memcpy(payload.data() + sizeof(meta), chunkMarkers.data(), chunkMarkers.size() * sizeof(AudioMarker));
		}
		std::memcpy(
			payload.data() + sizeof(meta) + chunkMarkers.size() * sizeof(AudioMarker),
			reinterpret_cast<const uint8_t*>(pcm.data()) + position,
			audioBytes
		);
		if (!sendMessage(AUDIO, generation, payload.data(), static_cast<uint32_t>(payload.size()))) return false;
		position = end;
		if (cancelled.load()) break;
	}
	return true;
}

bool applyAndSynth(const std::string& text, int rate, const std::map<int, int>& parameters) {
	espeakSetParameter(1, rate, 0);
	for (const auto& parameter : parameters) {
		if (parameter.first == 1) continue;
		espeakSetParameter(parameter.first, parameter.second, 0);
	}
	unsigned int uniqueId = 0;
	const unsigned int flags = ESPEAK_CHARS_UTF8 | ESPEAK_SSML;
	return espeakSynth(text.data(), 0, 0, 0, 0, flags, &uniqueId, nullptr) == 0;
}

bool runPhoSynthesis(
	uint32_t generation,
	const std::string& text,
	const std::map<int, int>& parameters,
	bool& audioSent
) {
	audioSent = false;
	if (!espeakSetPhonemeTrace || !ucrtFopen || !ucrtFclose) return false;
	std::string mbrolaExecutable;
	std::string database;
	if (!resolveMbrolaPaths(mbrolaExecutable, database)) return false;

	int targetRate = MBROLA_BASE_RATE;
	auto rate = parameters.find(1);
	if (rate != parameters.end()) targetRate = rate->second;
	targetRate = std::clamp(targetRate, 80, 1000);
	int volume = 100;
	auto volumeParameter = parameters.find(2);
	if (volumeParameter != parameters.end()) volume = volumeParameter->second;

	const std::string phoPath = makeTempPath(".pho");
	if (phoPath.empty()) return false;

	// Two eSpeak passes are needed. The first writes the .pho phone timing but
	// reports no useful audio positions, so a second pass collects the index
	// marks with real times. Both passes run at the base rate, so the marks map
	// onto the adjusted .pho durations.
	bool ok = false;
	collectProbeMarkers = false;
	espeakSetSynthCallback(probeCallback);
	espeakSetPhonemeTrace(0, nullptr);
	FILE* phoFile = ucrtFopen(phoPath.c_str(), "wb");
	if (phoFile) {
		espeakSetPhonemeTrace(0x10, phoFile);
		ok = applyAndSynth(text, MBROLA_BASE_RATE, parameters) && !cancelled.load();
		espeakSetPhonemeTrace(0, nullptr);
		ucrtFclose(phoFile);
	}
	if (ok && !cancelled.load()) {
		probeMarkers.clear();
		collectProbeMarkers = true;
		ok = applyAndSynth(text, MBROLA_BASE_RATE, parameters) && !cancelled.load();
		collectProbeMarkers = false;
	}
	espeakSetSynthCallback(synthesisCallback);

	std::vector<PhoLine> lines;
	std::vector<int> originalCumulative;
	std::vector<int> adjustedCumulative;
	std::string adjustedText;
	if (ok) {
		ok = adjustPho(
			phoPath,
			targetRate,
			lines,
			originalCumulative,
			adjustedCumulative,
			adjustedText
		);
	}

	if (ok && std::getenv("ESPEAK_MB_DUMP_PHO")) {
		std::ifstream baseSource(phoPath, std::ios::binary);
		std::ofstream baseDestination(engineDirectory + "\\host_base.pho", std::ios::binary);
		baseDestination << baseSource.rdbuf();
		std::ofstream adjustedDestination(engineDirectory + "\\host_adjusted.pho", std::ios::binary);
		adjustedDestination << adjustedText;
	}

	const bool forceExecutable = std::getenv("ESPEAK_MB_MBROLA_EXE") != nullptr;
	int sampleRate = 0;
	size_t totalBytes = 0;
	std::vector<int16_t> pcm;
	if (ok && !cancelled.load()) {
		// The marker pass ran this utterance through MBROLA, so the DLL already
		// holds the voice and only its leftover state has to be cleared. Loading
		// the database again would be wasted work.
		if (!forceExecutable && mbrolaDllAvailable() && prepareMbrolaEngine(database)) {
			sampleRate = mbrolaGetFreq();
			if (sampleRate <= 0) sampleRate = currentSampleRate.load();
			// MBROLA grows the output as it goes, so clamp marks against a
			// generous upper bound and let the sender place the remainder.
			totalBytes = std::numeric_limits<size_t>::max() / 4;
		} else {
			const std::string adjustedPath = makeTempPath(".pho");
			const std::string wavPath = makeTempPath(".wav");
			if (adjustedPath.empty() || wavPath.empty() || mbrolaExecutable.empty()) {
				DeleteFileA(adjustedPath.c_str());
				DeleteFileA(wavPath.c_str());
				DeleteFileA(phoPath.c_str());
				return false;
			}
			{
				std::ofstream adjustedFile(adjustedPath, std::ios::binary);
				adjustedFile << adjustedText;
			}
			ok = runMbrola(mbrolaExecutable, database, adjustedPath, wavPath);
			if (ok && !cancelled.load()) ok = readWav(wavPath, sampleRate, pcm, volume);
			DeleteFileA(adjustedPath.c_str());
			DeleteFileA(wavPath.c_str());
			if (ok) totalBytes = pcm.size() * sizeof(int16_t);
		}
	}
	if (ok && sampleRate > 0) currentSampleRate.store(sampleRate);

	std::vector<AudioMarker> markers;
	if (ok && sampleRate > 0) {
		for (const auto& marker : probeMarkers) {
			const double adjustedMs = mapMarkerTime(
				marker.timeMs, lines, originalCumulative, adjustedCumulative
			);
			int64_t offset = static_cast<int64_t>(adjustedMs * sampleRate * sizeof(int16_t) / 1000.0);
			offset = std::clamp<int64_t>(offset, 0, static_cast<int64_t>(totalBytes));
			offset &= ~int64_t(1);
			markers.push_back({marker.index, static_cast<uint32_t>(offset)});
		}
		std::sort(markers.begin(), markers.end(), [](const AudioMarker& left, const AudioMarker& right) {
			return left.byteOffset < right.byteOffset;
		});
	}

	if (ok && !cancelled.load()) {
		if (!pcm.empty()) {
			ok = sendPhoAudio(generation, sampleRate, pcm, markers);
		} else {
			// Keep exactly the audio that the adjusted timing model describes and
			// drop MBROLA's extra release tail.
			const double gain = phoVolumeGain() * std::clamp(volume, 0, 100) / 100.0;
			const size_t maxSamples = adjustedCumulative.empty()
				? 0
				: static_cast<size_t>(
					static_cast<double>(adjustedCumulative.back()) * sampleRate / 1000.0
				);
			ok = streamMbrolaAudio(generation, adjustedText, sampleRate, markers, gain, maxSamples);
		}
		audioSent = ok;
	}

	DeleteFileA(phoPath.c_str());
	return ok && audioSent;
}

void sendResponse(uint32_t requestId, int status, const EspeakVoice* voice = nullptr) {
	std::ostringstream out;
	out << "{\"status\":" << status << ",\"id\":\""
		<< jsonEscape(voice && voice->identifier ? basename(voice->identifier).c_str() : "") << "\"}";
	sendText(RESPONSE, requestId, out.str());
}

void runSynthesis(uint32_t generation, std::string text) {
	activeGeneration.store(generation);
	utteranceBytesSent = 0;
	std::map<int, int> parameters;
	{
		std::lock_guard<std::mutex> lock(parameterMutex);
		parameters = pendingParameters;
	}
	int status = 0;
	bool audioSent = false;
	{
		std::lock_guard<std::mutex> lock(engineMutex);
		if (!cancelled.load()) {
			runPhoSynthesis(generation, text, parameters, audioSent);
		}
		if (!audioSent && !cancelled.load()) {
			for (const auto& parameter : parameters) {
				// eSpeak NG rejects parameters that classic eSpeak accepted (for example
				// punctuation, capitals, and options). A rejected parameter is not fatal.
				espeakSetParameter(parameter.first, parameter.second, 0);
			}
			unsigned int uniqueId = 0;
			const unsigned int flags = ESPEAK_CHARS_UTF8 | ESPEAK_SSML | ESPEAK_PHONEMES;
			const int result = espeakSynth(text.data(), 0, 0, 0, 0, flags, &uniqueId, nullptr);
			if (result != 0) status = result;
		}
	}
	if (cancelled.load()) {
		sendMessage(CANCELLED, generation, nullptr, 0);
	} else if (status != 0) {
		sendText(ERROR_MESSAGE, generation, "espeak_Synth failed with code " + std::to_string(status));
		sendMessage(DONE, generation, nullptr, 0);
	} else {
		sendMessage(DONE, generation, nullptr, 0);
	}
}

bool processCommand(const Header& header, const std::vector<uint8_t>& payload, std::thread& synthesisThread) {
	if (header.type == SET_PARAMETER && payload.size() == sizeof(ParameterData)) {
		ParameterData data{};
		std::memcpy(&data, payload.data(), sizeof(data));
		std::lock_guard<std::mutex> lock(parameterMutex);
		pendingParameters[data.parameter] = data.value;
		return true;
	}
	if (header.type == SET_VOICE || header.type == SET_LANGUAGE) {
		cancelled.store(true);
		if (synthesisThread.joinable()) synthesisThread.join();
		cancelled.store(false);
		std::string value(payload.begin(), payload.end());
		int status = -1;
		{
			std::lock_guard<std::mutex> lock(engineMutex);
			if (header.type == SET_VOICE) {
				status = espeakSetVoiceByName(value.c_str());
			} else {
				EspeakVoice properties{};
				properties.languages = value.data();
				status = espeakSetVoiceByProperties(&properties);
			}
			sendResponse(header.generation, status, espeakGetCurrentVoice());
		}
		return true;
	}
	if (header.type == SPEAK) {
		cancelled.store(true);
		if (synthesisThread.joinable()) synthesisThread.join();
		cancelled.store(false);
		std::string text(payload.begin(), payload.end());
		activeGeneration.store(header.generation);
		synthesisThread = std::thread(runSynthesis, header.generation, std::move(text));
		return true;
	}
	if (header.type == CANCEL) {
		const uint32_t active = activeGeneration.load();
		if (header.generation == 0 || header.generation == active) cancelled.store(true);
		return true;
	}
	return false;
}

void shutdown(std::thread& synthesisThread) {
	cancelled.store(true);
	if (synthesisThread.joinable()) synthesisThread.join();
	closeMbrolaEngine();
	if (espeakTerminate) espeakTerminate();
	if (espeakModule) FreeLibrary(espeakModule);
	if (mbrolaModule) FreeLibrary(mbrolaModule);
	SetDllDirectoryA(nullptr);
}

} // namespace

int main(int argc, char** argv) {
	_setmode(_fileno(stdin), _O_BINARY);
	_setmode(_fileno(stdout), _O_BINARY);
	if (argc != 2) return 2;
	inputHandle = GetStdHandle(STD_INPUT_HANDLE);
	outputHandle = GetStdHandle(STD_OUTPUT_HANDLE);
	std::string error;
	if (!initializeEngine(argv[1], error)) {
		sendText(ERROR_MESSAGE, 0, error);
		std::thread noSynthesis;
		shutdown(noSynthesis);
		return 1;
	}
	espeakSetSynthCallback(synthesisCallback);
	for (int parameter = 1; parameter <= 9; ++parameter) {
		pendingParameters[parameter] = espeakGetParameter(parameter, 1);
	}
	const int sampleRate = currentSampleRate.load();
	const std::string ready = buildReadyJson(sampleRate);
	if (!sendText(READY, 0, ready)) {
		std::thread noSynthesis;
		shutdown(noSynthesis);
		return 1;
	}

	std::thread synthesisThread;
	while (true) {
		Header header{};
		if (!readExact(&header, sizeof(header))) break;
		if (header.magic != MAGIC || header.size > MAX_PACKET_SIZE) break;
		std::vector<uint8_t> payload(header.size);
		if (header.size && !readExact(payload.data(), header.size)) break;
		if (header.type == QUIT) break;
		if (!processCommand(header, payload, synthesisThread)) {
			sendText(ERROR_MESSAGE, header.generation, "Unknown or malformed helper command");
		}
	}
	shutdown(synthesisThread);
	return 0;
}
