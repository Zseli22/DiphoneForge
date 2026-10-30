/* annaunits: what Anna's recorded inventory actually contains - the unit definition table (M1033DSK.UDT)
 * and how the corpus units (M1033DSK.UNT) are spread over it.  Step one of building a Sam-format voice out
 * of her recordings: it decides whether her units can be cut into Sam's phone-sized pieces.
 *
 *   annaunits [--dir TTS20] [--list]        (--list prints every unit type with its corpus count)
 *
 * UNT layout (anna_voice.c load_unt): u32 count at 0x14, u32 type0[261] at 0x100 (first unit of each type,
 * so the type is the group a record falls in), records of 20 bytes from 0x514:
 *   u32 packed target features | u32 (bit0 join, bits 1..24 length in samples) | u32 start | u32 wih | u16 n
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "anna.h"
#include "anna_front.h"

#define DEF_DIR "C:/Program Files (x86)/Common Files/SpeechEngines/Microsoft/TTS20"

static unsigned rd32(const unsigned char *p) { return p[0] | p[1] << 8 | p[2] << 16 | (unsigned)p[3] << 24; }

int main(int argc, char **argv)
{
    const char *dir = getenv("ANNA_DIR") ? getenv("ANNA_DIR") : DEF_DIR;
    char path[1024], err[256];
    anna_udt *u;
    FILE *f;
    unsigned char *d;
    long sz;
    int i, t, list = 0, n, nunits;
    long type0[262];
    int *count;
    double *dur;
    const char **name;
    int nph[8];
    double total = 0;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--dir") && i + 1 < argc) dir = argv[++i];
        else if (!strcmp(argv[i], "--list")) list = 1;
    }
    snprintf(path, sizeof path, "%s/en-US/enu-dsk/M1033DSK.UDT", dir);
    u = anna_udt_load(path, err, sizeof err);
    if (!u) { fprintf(stderr, "%s\n", err); return 1; }
    n = anna_udt_count(u);

    snprintf(path, sizeof path, "%s/en-US/enu-dsk/M1033DSK.UNT", dir);
    f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "cannot read %s\n", path); return 1; }
    fseek(f, 0, SEEK_END);
    sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    d = malloc((size_t)sz);
    if (!d || fread(d, 1, (size_t)sz, f) != (size_t)sz) return 1;
    fclose(f);
    nunits = (int)rd32(d + 0x14);
    for (i = 0; i < 261; i++) type0[i] = (long)rd32(d + 0x100 + 4 * i);
    type0[261] = nunits;

    /* type index (the UNT group) -> name, from the UDT */
    name = calloc(262, sizeof *name);
    for (i = 0; i < n; i++) {
        int k = anna_udt_index(u, i);
        if (k >= 0 && k < 262) name[k] = anna_udt_name(u, i);
    }
    count = calloc(262, sizeof *count);
    dur = calloc(262, sizeof *dur);
    for (t = 0; t < 261; t++)
        for (i = (int)type0[t]; i < (int)type0[t + 1] && i < nunits; i++) {
            const unsigned char *r = d + 0x514 + (size_t)20 * i;
            unsigned l = rd32(r + 4);
            count[t]++;
            dur[t] += (double)((l >> 1) & 0xFFFFFF);
        }
    for (t = 0; t < 261; t++) total += dur[t];
    printf("%d unit types in the UDT, %d corpus units, %.2f h of recordings\n", n, nunits, total / 16000.0 / 3600.0);

    memset(nph, 0, sizeof nph);
    for (t = 0; t < 261; t++) {
        const char *nm = name[t];
        int k = 1;
        const char *p;
        if (!count[t]) continue;
        if (!nm) { nph[7]++; continue; }
        for (p = nm; *p; p++)
            if (*p == '+') k++;
        if (nm[0] == '_') k = 0;
        nph[k < 7 ? k : 6]++;
    }
    printf("used types by phone count: letters %d, 1 phone %d, 2 %d, 3 %d, 4 %d, 5+ %d, unnamed %d\n", nph[0],
           nph[1], nph[2], nph[3], nph[4], nph[5] + nph[6], nph[7]);
    if (list) {
        printf("\n%-6s %-24s %8s %10s %9s\n", "type", "name", "count", "samples", "avg ms");
        for (t = 0; t < 261; t++)
            if (count[t])
                printf("%-6d %-24s %8d %10.0f %9.1f\n", t, name[t] ? name[t] : "?", count[t], dur[t],
                       dur[t] / count[t] / 16.0);
    }
    free(count);
    free(dur);
    free((void *)name);
    free(d);
    anna_udt_free(u);
    return 0;
}
