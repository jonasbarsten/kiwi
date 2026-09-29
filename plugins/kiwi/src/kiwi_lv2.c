#include <stdlib.h>
#include <lv2/core/lv2.h>
#include "kiwi_dsp.h"

#define KIWI_URI "https://github.com/jonasbarsten/kiwi#"

typedef struct {
    kiwi_mix dsp;
    float rate;
    const float *in[6];
    float *out[4];
    const float *ctl[6];
} mix_plugin;

static LV2_Handle mix_instantiate(const LV2_Descriptor *d, double rate,
                                  const char *path, const LV2_Feature *const *features) {
    (void)d; (void)path; (void)features;
    mix_plugin *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    p->rate = (float)rate;
    kiwi_mix_init(&p->dsp, p->rate);
    return p;
}

static void mix_connect(LV2_Handle h, uint32_t port, void *data) {
    mix_plugin *p = h;
    if (port < 6) p->in[port] = data;
    else if (port < 10) p->out[port - 6] = data;
    else if (port < 16) p->ctl[port - 10] = data;
}

static void mix_activate(LV2_Handle h) {
    mix_plugin *p = h;
    kiwi_mix_init(&p->dsp, p->rate);
}

static void mix_run(LV2_Handle h, uint32_t n) {
    mix_plugin *p = h;
    float vol[KIWI_MODES], send[KIWI_MODES];
    for (int k = 0; k < KIWI_MODES; k++) {
        vol[k] = *p->ctl[k * 2];
        send[k] = *p->ctl[k * 2 + 1];
    }
    kiwi_mix_process(&p->dsp, p->in, p->out, vol, send, n);
}

typedef struct {
    kiwi_carrier dsp;
    float rate;
    const float *in[6];
    float *out;
    const float *blend;
} carrier_plugin;

static LV2_Handle carrier_instantiate(const LV2_Descriptor *d, double rate,
                                      const char *path, const LV2_Feature *const *features) {
    (void)d; (void)path; (void)features;
    carrier_plugin *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    p->rate = (float)rate;
    kiwi_carrier_init(&p->dsp, p->rate);
    return p;
}

static void carrier_connect(LV2_Handle h, uint32_t port, void *data) {
    carrier_plugin *p = h;
    if (port < 6) p->in[port] = data;
    else if (port == 6) p->out = data;
    else if (port == 7) p->blend = data;
}

static void carrier_activate(LV2_Handle h) {
    carrier_plugin *p = h;
    kiwi_carrier_init(&p->dsp, p->rate);
}

static void carrier_run(LV2_Handle h, uint32_t n) {
    carrier_plugin *p = h;
    kiwi_carrier_process(&p->dsp, p->in, p->out, *p->blend, n);
}

static void cleanup(LV2_Handle h) { free(h); }

static const void *extension_data(const char *uri) { (void)uri; return NULL; }

static const LV2_Descriptor descriptors[] = {
    { KIWI_URI "mix", mix_instantiate, mix_connect, mix_activate, mix_run,
      NULL, cleanup, extension_data },
    { KIWI_URI "carrier", carrier_instantiate, carrier_connect, carrier_activate, carrier_run,
      NULL, cleanup, extension_data },
};

LV2_SYMBOL_EXPORT const LV2_Descriptor *lv2_descriptor(uint32_t index) {
    return index < 2 ? &descriptors[index] : NULL;
}
