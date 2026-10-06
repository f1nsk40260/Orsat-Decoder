/* Orsat-Decoder : décodeur WSPR (créneau de 2 minutes), autour de wsprd (K1JT, K9AN, VA2GKA, GPL-3).

   Décodage : wspr_decode_iq fichier.iq [fréquence_cadran_Hz]
     fichier.iq : flottants 32 bits I,Q entrelacés à 375 Hz, bande WSPR centrée sur 0 Hz
     (fréquence audio 1500 Hz). Une ligne par message : snr dt freq_hz drift indicatif locator puissance
   Symboles (tests) : wspr_decode_iq -e "F1NSK JN03 30"  -> les 162 symboles de canal (0..3)
   Fano (JT9, même code K=32) : wspr_decode_iq -f [nbits] < 2*nbits octets souples (0..255, 128 = doute)
     -> les octets décodés en hexadécimal et la métrique, ou FAIL */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "wsprd.h"
#include "wsprsim_utils.h"
#include "fano.h"

extern float metric_tables[5][256];

static int fano_mode(int nbits) {
    unsigned char sym[2 * 128];
    if (nbits < 1 || nbits > 128 || fread(sym, 1, 2 * nbits, stdin) != (size_t)(2 * nbits))
        return 1;
    int mettab[2][256];
    float bias = 0.45f;
    for (int i = 0; i < 256; i++) {
        mettab[0][i] = (int)roundf(10.0f * (metric_tables[2][i] - bias));
        mettab[1][i] = (int)roundf(10.0f * (metric_tables[2][255 - i] - bias));
    }
    unsigned char data[17] = {0};
    unsigned int metric = 0, cycles = 0, maxnp = 0;
    if (fano(&metric, &cycles, &maxnp, data, sym, nbits, mettab, 60, 10000)) {
        printf("FAIL\n");
        return 0;
    }
    for (int i = 0; i < (nbits + 7) / 8; i++)
        printf("%02x", data[i]);
    printf(" %u\n", metric);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s fichier.iq [cadran_Hz] | -e \"CALL LOC PWR\"\n", argv[0]);
        return 1;
    }
    if (strcmp(argv[1], "-f") == 0)
        return fano_mode(argc > 2 ? atoi(argv[2]) : 103);
    if (strcmp(argv[1], "-e") == 0 && argc > 2) {
        static char hashtab[HASHTAB_SIZE * HASHTAB_ENTRY_LEN];
        static char loctab[HASHTAB_SIZE * LOCTAB_ENTRY_LEN];
        unsigned char sym[162];
        char msg[64];
        snprintf(msg, sizeof(msg), "%s", argv[2]);
        if (!get_wspr_channel_symbols(msg, hashtab, loctab, sym))
            return 2;
        for (int i = 0; i < 162; i++)
            printf("%d", sym[i]);
        printf("\n");
        return 0;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) {
        perror(argv[1]);
        return 1;
    }
    int cap = 375 * 120;
    float *iq = calloc(2 * cap, sizeof(float));
    int n = (int)fread(iq, sizeof(float) * 2, cap, f);
    fclose(f);
    float *I = calloc(cap, sizeof(float)), *Q = calloc(cap, sizeof(float));
    for (int i = 0; i < n; i++) {
        I[i] = iq[2 * i];
        Q[i] = iq[2 * i + 1];
    }
    struct decoder_options opt;
    memset(&opt, 0, sizeof(opt));
    opt.freq = argc > 2 ? atoi(argv[2]) : 0;
    snprintf(opt.rcall, sizeof(opt.rcall), "ORSAT");
    snprintf(opt.rloc, sizeof(opt.rloc), "JN03");
    opt.quickmode = 0;
    opt.usehashtable = 0;
    opt.npasses = 2;
    opt.subtraction = 1;
    struct decoder_results res[50];
    int nres = 0;
    wspr_decode(I, Q, cap, opt, res, &nres);
    for (int i = 0; i < nres; i++)
        printf("%.0f %.1f %.1f %.0f %s %s %s\n", res[i].snr, res[i].dt, res[i].freq * 1e6 - opt.freq, res[i].drift,
               res[i].call, res[i].loc, res[i].pwr);
    return 0;
}
