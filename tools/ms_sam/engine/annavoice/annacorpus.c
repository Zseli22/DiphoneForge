/* annacorpus: dump Anna's recorded corpus in a form the voice builder can work on.
 *
 * For every one of her 159043 corpus units it writes the decoded audio, the pitch epochs from the .WIH and
 * the label (unit type = a phone group like "w+aa+n"), so the builder can cut phones out of them and analyse
 * them pitch-synchronously.
 *
 *   annacorpus [--dir TTS20] [--out DIR] [--limit N]
 *
 * Writes into DIR (default "corpus"):
 *   units.bin   "ANCP" u32 1, u32 nunits, then per unit:
 *                 i32 type, i32 start, i32 len, i64 pcm_off (samples), i32 nep, i64 ep_off, 9 bytes features, 3 pad
 *   audio.pcm   16-bit mono 16 kHz, the units in order
 *   epochs.i16  one int16 per epoch: the period length in samples, negative = unvoiced
 *   types.txt   "index name" per unit type
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <direct.h>
#else
#include <sys/stat.h>
#endif

#include "anna.h"
#include "anna_front.h"

#define DEF_DIR "C:/Program Files (x86)/Common Files/SpeechEngines/Microsoft/TTS20"

static unsigned rd32(const unsigned char *p) { return p[0] | p[1] << 8 | p[2] << 16 | (unsigned)p[3] << 24; }

static void wr32(FILE *f, unsigned v) { fputc(v & 255, f); fputc(v >> 8 & 255, f); fputc(v >> 16 & 255, f); fputc(v >> 24 & 255, f); }
static void wr64(FILE *f, long long v) { wr32(f, (unsigned)v); wr32(f, (unsigned)(v >> 32)); }

static int load_key(const char *dir, unsigned char key[129])
{
    char fn[1024];
    FILE *f;
    int ok;
    snprintf(fn, sizeof fn, "%s/MSTTSDecWrp.dll", dir);
    f = fopen(fn, "rb");
    if (!f) return -1;
    fseek(f, 0x13c8, SEEK_SET);
    ok = fread(key, 1, 129, f) == 129 && !memcmp(key, "wyClImqD", 8);
    fclose(f);
    return ok ? 0 : -1;
}

/* the .WIH epoch bytes are deltas with 127 / -128 as continuation escapes (anna_render.c psola) */
static int decode_epochs(const signed char *b, int n, short *out, int max)
{
    int i, m = 0;
    for (i = 0; i < n && m < max; i++) {
        int x = b[i];
        if (x == 127) {
            while (++i < n) {
                x += (unsigned char)b[i];
                if ((unsigned char)b[i] != 0x7f) break;
            }
        } else if (x == -128) {
            while (++i < n) {
                x += b[i];
                if (b[i] != -128) break;
            }
        }
        out[m++] = (short)(x > 32767 ? 32767 : x < -32768 ? -32768 : x);
    }
    return m;
}

int main(int argc, char **argv)
{
    const char *dir = getenv("ANNA_DIR") ? getenv("ANNA_DIR") : DEF_DIR, *outdir = "corpus";
    char path[1024], err[256];
    unsigned char key[129];
    anna_decoder *dec;
    anna_udt *udt;
    FILE *fu, *fa, *fe, *fi, *ft, *fw;
    unsigned char *d;
    long sz;
    long type0[262];
    const char *name[262];
    int i, t, nunits, limit = 0, n;
    long long pcm_off = 0, ep_off = 0;
    short *pcm = NULL;
    signed char *wbuf = NULL;
    short *eps = NULL;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--dir") && i + 1 < argc) dir = argv[++i];
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) outdir = argv[++i];
        else if (!strcmp(argv[i], "--limit") && i + 1 < argc) limit = atoi(argv[++i]);
    }
    if (load_key(dir, key)) { fprintf(stderr, "annacorpus: cannot read the voice key\n"); return 1; }
    snprintf(path, sizeof path, "%s/en-US/enu-dsk/M1033DSK.CSD", dir);
    {
        char idxp[1024];
        snprintf(idxp, sizeof idxp, "%s/en-US/enu-dsk/M1033DSK.IDX", dir);
        dec = anna_decoder_open(path, idxp, key, err, sizeof err);
    }
    if (!dec) { fprintf(stderr, "annacorpus: %s\n", err); return 1; }
    snprintf(path, sizeof path, "%s/en-US/enu-dsk/M1033DSK.UDT", dir);
    udt = anna_udt_load(path, err, sizeof err);
    if (!udt) { fprintf(stderr, "annacorpus: %s\n", err); return 1; }

    snprintf(path, sizeof path, "%s/en-US/enu-dsk/M1033DSK.UNT", dir);
    fu = fopen(path, "rb");
    if (!fu) { fprintf(stderr, "cannot read %s\n", path); return 1; }
    fseek(fu, 0, SEEK_END);
    sz = ftell(fu);
    fseek(fu, 0, SEEK_SET);
    d = malloc((size_t)sz);
    if (!d || fread(d, 1, (size_t)sz, fu) != (size_t)sz) return 1;
    fclose(fu);
    nunits = (int)rd32(d + 0x14);
    for (i = 0; i < 261; i++) type0[i] = (long)rd32(d + 0x100 + 4 * i);
    type0[261] = nunits;
    if (limit > 0 && limit < nunits) nunits = nunits; /* limit applies to units written, see below */

    snprintf(path, sizeof path, "%s/en-US/enu-dsk/M1033DSK.WIH", dir);
    fw = fopen(path, "rb");
    if (!fw) { fprintf(stderr, "cannot read %s\n", path); return 1; }

    memset(name, 0, sizeof name);
    for (i = 0; i < anna_udt_count(udt); i++) {
        int k = anna_udt_index(udt, i);
        if (k >= 0 && k < 262) name[k] = anna_udt_name(udt, i);
    }
#ifdef _WIN32
    _mkdir(outdir);
#else
    mkdir(outdir, 0777);
#endif
    snprintf(path, sizeof path, "%s/types.txt", outdir);
    ft = fopen(path, "wb");
    for (t = 0; t < 261; t++) fprintf(ft, "%d %s\n", t, name[t] ? name[t] : "?");
    fclose(ft);

    snprintf(path, sizeof path, "%s/units.bin", outdir);
    fi = fopen(path, "wb");
    snprintf(path, sizeof path, "%s/audio.pcm", outdir);
    fa = fopen(path, "wb");
    snprintf(path, sizeof path, "%s/epochs.i16", outdir);
    fe = fopen(path, "wb");
    if (!fi || !fa || !fe) { fprintf(stderr, "cannot write into %s\n", outdir); return 1; }
    fwrite("ANCP", 1, 4, fi);
    wr32(fi, 1);
    wr32(fi, (unsigned)nunits);

    pcm = malloc(sizeof(short) * (1 << 22));
    wbuf = malloc(1 << 20);
    eps = malloc(sizeof(short) * (1 << 20));
    n = 0;
    for (t = 0; t < 261; t++) {
        for (i = (int)type0[t]; i < (int)type0[t + 1] && i < nunits; i++) {
            const unsigned char *r = d + 0x514 + (size_t)20 * i;
            unsigned fpack = rd32(r), l = rd32(r + 4);
            int len = (int)((l >> 1) & 0xFFFFFF), start = (int)rd32(r + 8);
            unsigned woff = rd32(r + 12);
            int wn = r[16] | r[17] << 8, nep = 0, k;
            static const int sh[9] = {0, 4, 7, 10, 16, 22, 25, 28, 30}, mk[9] = {15, 7, 7, 63, 63, 7, 7, 3, 3};
            if (limit > 0 && n >= limit) goto done;
            if (len <= 0 || len > (1 << 22)) continue;
            if (anna_decoder_read(dec, start, len, pcm)) {
                fprintf(stderr, "decode failed at unit %d\n", i);
                continue;
            }
            if (wn > 0 && wn <= (1 << 20) && !fseek(fw, 16 + (long)woff, SEEK_SET) &&
                fread(wbuf, 1, (size_t)wn, fw) == (size_t)wn)
                nep = decode_epochs(wbuf, wn, eps, 1 << 20);
            wr32(fi, (unsigned)t);
            wr32(fi, (unsigned)start);
            wr32(fi, (unsigned)len);
            wr64(fi, pcm_off);
            wr32(fi, (unsigned)nep);
            wr64(fi, ep_off);
            for (k = 0; k < 9; k++) fputc((int)((fpack >> sh[k]) & (unsigned)mk[k]), fi);
            for (k = 0; k < 3; k++) fputc(0, fi);
            fwrite(pcm, 2, (size_t)len, fa);
            if (nep) fwrite(eps, 2, (size_t)nep, fe);
            pcm_off += len;
            ep_off += nep;
            n++;
            if (!(n % 10000)) fprintf(stderr, "  %d units, %.2f h\r", n, pcm_off / 16000.0 / 3600.0);
        }
    }
done:
    fclose(fa);
    fclose(fe);
    /* the real count goes back into the header */
    fseek(fi, 8, SEEK_SET);
    wr32(fi, (unsigned)n);
    fclose(fi);
    fclose(fw);
    printf("\n%s: %d units, %.2f h of audio, %lld epochs\n", outdir, n, pcm_off / 16000.0 / 3600.0, ep_off);
    free(pcm);
    free(wbuf);
    free(eps);
    free(d);
    anna_udt_free(udt);
    anna_decoder_close(dec);
    return 0;
}
