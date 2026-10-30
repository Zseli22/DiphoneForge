/* samleaf: dump Sam's unit tree as a table, so the voice builder can bucket Anna's recordings the way
 * Sam's front end buckets phones.
 *
 * For every (phone, left, right, position) the front end walks a CART and ends at one of the 3365 units;
 * this asks it for every combination and writes the answers, plus the phone names.
 *
 *   samleaf Sam.spd out.bin
 *
 * out.bin: "SLF1", i32 nphones, i32 nunits, then nphones*nphones*nphones*4 int32 unit indices in the order
 * phone, left, right, position (positions 1, 2, 4, 8 = the front end's posbits), then one int32 per phone
 * with its first unit index, then the phone names, NUL-separated.
 *
 * It includes sam_front.c because the tree walk is private to it; only t->voice is needed.
 */
#include "sam_front.c"

static unsigned rdu32(const unsigned char *p) { return p[0] | p[1] << 8 | p[2] << 16 | (unsigned)p[3] << 24; }

int main(int argc, char **argv)
{
    char err[256];
    struct sam_tts t;
    sam_voice *v;
    FILE *f;
    const uint8_t *s0, *s2;
    uint32_t size;
    int nph, i, l, r, pb, base_sub;
    static const int POS[4] = {1, 2, 4, 8};

    if (argc < 3) {
        fprintf(stderr, "usage: samleaf Sam.spd out.bin\n");
        return 1;
    }
    v = sam_voice_load(argv[1], err, sizeof err);
    if (!v) {
        fprintf(stderr, "samleaf: %s\n", err);
        return 1;
    }
    memset(&t, 0, sizeof t);
    t.voice = v;
    s0 = sam_voice_section(v, 0, &size);
    nph = (int)rdu32(s0 + 0x24);
    base_sub = (int)rdu32(s0 + 9 * 4);
    s2 = sam_voice_section(v, 2, &size);

    f = fopen(argv[2], "wb");
    if (!f) {
        fprintf(stderr, "cannot write %s\n", argv[2]);
        return 1;
    }
    fwrite("SLF1", 1, 4, f);
    {
        int32_t hdr[2];
        hdr[0] = nph;
        hdr[1] = sam_voice_units(v);
        fwrite(hdr, 4, 2, f);
    }
    for (i = 0; i < nph; i++)
        for (l = 0; l < nph; l++)
            for (r = 0; r < nph; r++)
                for (pb = 0; pb < 4; pb++) {
                    int leaf = tree_leaf(&t, i, l, r, POS[pb]);
                    int32_t unit = leaf < 0 ? -1 : (int32_t)(rdu32(s2 + 0x34 + i * 4) - base_sub + 1 + leaf);
                    fwrite(&unit, 4, 1, f);
                }
    for (i = 0; i < nph; i++) { /* where each phone's leaves start in the unit numbering */
        int32_t base = (int32_t)(rdu32(s2 + 0x34 + i * 4) - base_sub + 1);
        fwrite(&base, 4, 1, f);
    }
    for (i = 0; i < nph; i++) {
        const char *nm = (const char *)(s0 + rdu32(s0 + rdu32(s0 + 0x1C) + 4 * i));
        fwrite(nm, 1, strlen(nm) + 1, f);
    }
    fclose(f);
    printf("%s: %d phones, %d units, %d combinations\n", argv[2], nph, sam_voice_units(v), nph * nph * nph * 4);
    sam_voice_free(v);
    return 0;
}
