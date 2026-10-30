/* Ask the installed eSpeak for what it decided about a piece of text.
 *
 * Two files come out: <prefix>.pho is what eSpeak would hand MBROLA, and
 * <prefix>.mn is the same utterance as phoneme mnemonics, which is the only
 * place the stress marks are written down.  The mnemonics are what the pitch
 * model needs to know which syllables carry an accent; the .pho is where its
 * answer goes back.
 *
 * Build, from the project root, with the 32-bit eSpeak the add-on uses:
 *
 *   gcc -m32 -O2 -o tools/pitch_model/pho_dump.exe tools/pitch_model/pho_dump.c
 *
 * Usage:
 *
 *   pho_dump.exe <engineDir> <voice> <rate> <prefix> <text...>
 *
 * <engineDir> is the synthDrivers folder of the add-on; the DLL and the data
 * are both looked for under it.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

typedef int  (__cdecl *Initialize)(int, int, const char *, int);
typedef int  (__cdecl *SetVoiceByName)(const char *);
typedef int  (__cdecl *SetParameter)(int, int, int);
typedef void (__cdecl *SetPhonemeTrace)(int, FILE *);
typedef int  (__cdecl *Synth)(const void *, int, unsigned int, int, unsigned int,
                              unsigned int, const void *, void *);
typedef int  (__cdecl *Synchronize)(void);
typedef char *(__cdecl *TextToPhonemes)(const void **, int, int);
typedef int  (__cdecl *SynthCallback)(short *, int, void *);
typedef void (__cdecl *SetSynthCallback)(SynthCallback);

/* The DLL writes the trace through the Universal CRT it is built against,
   not through the one this program is linked with, and a FILE * from one is
   not a FILE * to the other.  So the trace file has to be opened and closed
   by the same CRT the DLL will write to. */
typedef FILE *(__cdecl *UcrtFopen)(const char *, const char *);
typedef int   (__cdecl *UcrtFclose)(FILE *);

#define ESPEAK_RATE 1
#define POS_CHARACTER 1
#define CHARS_UTF8 1

static int __cdecl discard(short *wav, int n, void *ev)
{
    (void)wav; (void)n; (void)ev;
    return 0;
}

int main(int argc, char **argv)
{
    char dllPath[1024], phoPath[1024], mnPath[1024], text[8192];
    HMODULE dll;
    Initialize init;
    SetVoiceByName setVoice;
    SetParameter setParam;
    SetPhonemeTrace setTrace;
    Synth synth;
    Synchronize sync;
    TextToPhonemes ttp;
    SetSynthCallback setCallback;
    UcrtFopen  ucrtFopen;
    UcrtFclose ucrtFclose;
    HMODULE ucrt;
    FILE *pho;
    int i;

    if (argc < 6) {
        fprintf(stderr,
                "usage: %s <engineDir> <voice> <rate> <prefix> <text...>\n",
                argv[0]);
        return 2;
    }

    snprintf(dllPath, sizeof dllPath, "%s\\espeak.dll", argv[1]);
    dll = LoadLibraryA(dllPath);
    if (!dll) {
        fprintf(stderr, "cannot load %s (%lu)\n", dllPath, GetLastError());
        return 1;
    }

    init      = (Initialize)GetProcAddress(dll, "espeak_Initialize");
    setVoice  = (SetVoiceByName)GetProcAddress(dll, "espeak_SetVoiceByName");
    setParam  = (SetParameter)GetProcAddress(dll, "espeak_SetParameter");
    setTrace  = (SetPhonemeTrace)GetProcAddress(dll, "espeak_SetPhonemeTrace");
    synth     = (Synth)GetProcAddress(dll, "espeak_Synth");
    sync      = (Synchronize)GetProcAddress(dll, "espeak_Synchronize");
    ttp       = (TextToPhonemes)GetProcAddress(dll, "espeak_TextToPhonemes");
    setCallback = (SetSynthCallback)GetProcAddress(dll, "espeak_SetSynthCallback");

    if (!init || !setVoice || !setParam || !setTrace || !synth || !sync ||
        !ttp || !setCallback) {
        fprintf(stderr, "the DLL is missing one of the calls this needs\n");
        return 1;
    }

    ucrt = LoadLibraryA("ucrtbase.dll");
    ucrtFopen  = ucrt ? (UcrtFopen)GetProcAddress(ucrt, "fopen") : NULL;
    ucrtFclose = ucrt ? (UcrtFclose)GetProcAddress(ucrt, "fclose") : NULL;
    if (!ucrtFopen || !ucrtFclose) {
        fprintf(stderr, "cannot reach the Universal CRT\n");
        return 1;
    }

    /* 2 is synchronous output: the samples go to the callback below and the
       call returns when the utterance is finished, which is what makes the
       .pho complete before this program reads it. */
    if (init(2, 300, argv[1], 0) <= 0) {
        fprintf(stderr, "init failed\n");
        return 1;
    }
    if (setVoice(argv[2]) != 0) {
        fprintf(stderr, "setvoice failed\n");
        return 1;
    }
    setParam(ESPEAK_RATE, atoi(argv[3]), 0);
    setCallback(discard);

    text[0] = 0;
    for (i = 5; i < argc; i++) {
        if (i > 5) strncat(text, " ", sizeof text - strlen(text) - 1);
        strncat(text, argv[i], sizeof text - strlen(text) - 1);
    }

    snprintf(phoPath, sizeof phoPath, "%s.pho", argv[4]);
    snprintf(mnPath,  sizeof mnPath,  "%s.mn",  argv[4]);

    pho = ucrtFopen(phoPath, "wb");
    if (!pho) {
        fprintf(stderr, "cannot write %s\n", phoPath);
        return 1;
    }
    setTrace(0x10, pho);
    {
        /* One call answers one clause and advances the pointer, so a
           sentence with a comma in it needs the loop. */
        const void *p = text;
        FILE *mf = fopen(mnPath, "wb");
        for (;;) {
            char *mn = ttp(&p, 1, 0);
            if (mn == NULL || *mn == 0)
                break;
            if (mf) fprintf(mf, "%s\n", mn);
        }
        if (mf) fclose(mf);
    }
    synth(text, (int)strlen(text) + 1, 0, POS_CHARACTER, 0, CHARS_UTF8,
          NULL, NULL);
    sync();
    setTrace(0, NULL);
    ucrtFclose(pho);

    return 0;
}
