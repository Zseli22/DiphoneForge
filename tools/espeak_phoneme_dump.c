#include <stdio.h>
#include <windows.h>

typedef int (__cdecl *Initialize)(int output, int buflength, const char *path, int options);
typedef int (__cdecl *SetVoiceByName)(const char *name);
typedef char *(__cdecl *TextToPhonemes)(const void **textptr, int textmode, int phonememode);

int main(int argc, char **argv)
{
    char path[1024];
    HMODULE dll;
    Initialize init;
    SetVoiceByName setvoice;
    TextToPhonemes ttp;
    int i;
    int mode = 0;

    if (argc > 4 && argv[3][0]==0x30 && argv[3][1]==0x78) { mode = (int)strtol(argv[3],0,0); i = 4; } else { i = 3; }
    if (argc < 4) { fprintf(stderr, "usage: %s <engineDir> <voice> <word>...\n", argv[0]); return 2; }
    snprintf(path, sizeof(path), "%s\\espeak.dll", argv[1]);
    dll = LoadLibraryA(path);
    if (!dll) { fprintf(stderr, "load failed %lu\n", GetLastError()); return 1; }
    init = (Initialize)GetProcAddress(dll, "espeak_Initialize");
    setvoice = (SetVoiceByName)GetProcAddress(dll, "espeak_SetVoiceByName");
    ttp = (TextToPhonemes)GetProcAddress(dll, "espeak_TextToPhonemes");
    if (!init || !setvoice || !ttp) { fprintf(stderr, "missing export\n"); return 1; }
    if (init(2, 300, argv[1], 0) <= 0) { fprintf(stderr, "init failed\n"); return 1; }
    if (setvoice(argv[2]) != 0) { fprintf(stderr, "setvoice failed\n"); return 1; }
    for (i = 3; i < argc; i++) {
        const void *text = argv[i];
        char *ph = ttp(&text, 1, mode);
        printf("%-12s %s\n", argv[i], ph ? ph : "(null)");
        fflush(stdout);
    }
    ExitProcess(0);
    return 0;
}
