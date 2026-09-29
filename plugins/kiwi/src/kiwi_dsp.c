#include <math.h>
#include "kiwi_dsp.h"

#define HALF_PI 1.57079632679f

void kiwi_smoother_init(kiwi_smoother *s, float sample_rate, float time_ms, float initial) {
    s->coef = 1.0f - expf(-1000.0f / (time_ms * sample_rate));
    s->value = initial;
    s->target = initial;
}

void kiwi_smoother_set(kiwi_smoother *s, float target) {
    s->target = target;
}

float kiwi_smoother_next(kiwi_smoother *s) {
    float delta = s->target - s->value;
    float next = s->value + delta * s->coef;
    /* Near the target the step drops below float precision and the value
     * would stall short of it; snap instead. */
    if (fabsf(delta) < 1e-6f || next == s->value)
        s->value = s->target;
    else
        s->value = next;
    return s->value;
}

static float clampf(float x, float lo, float hi) {
    return x < lo ? lo : (x > hi ? hi : x);
}

float kiwi_taper(float x) {
    x = clampf(x, 0.0f, 1.0f);
    return x * x * x;
}

void kiwi_carrier_gains(float blend, float *synth, float *piano, float *sampler) {
    blend = clampf(blend, 0.0f, 2.0f);
    *synth = *piano = *sampler = 0.0f;
    if (blend == 0.0f) { *synth = 1.0f; return; }
    if (blend == 1.0f) { *piano = 1.0f; return; }
    if (blend == 2.0f) { *sampler = 1.0f; return; }
    if (blend < 1.0f) {
        *synth = cosf(blend * HALF_PI);
        *piano = sinf(blend * HALF_PI);
    } else {
        *piano = cosf((blend - 1.0f) * HALF_PI);
        *sampler = sinf((blend - 1.0f) * HALF_PI);
    }
}

void kiwi_mix_init(kiwi_mix *m, float sample_rate) {
    for (int k = 0; k < KIWI_MODES; k++) {
        kiwi_smoother_init(&m->vol[k], sample_rate, KIWI_SMOOTH_MS, 0.0f);
        kiwi_smoother_init(&m->send[k], sample_rate, KIWI_SMOOTH_MS, 0.0f);
    }
    m->sample_rate = sample_rate;
    m->primed = 0;
}

void kiwi_mix_process(kiwi_mix *m, const float **in, float **out,
                      const float *vol, const float *send, uint32_t n) {
    /* The first block starts at the knob positions instead of fading in from 0. */
    for (int k = 0; k < KIWI_MODES; k++) {
        if (!m->primed) {
            kiwi_smoother_init(&m->vol[k], m->sample_rate, KIWI_SMOOTH_MS, vol[k]);
            kiwi_smoother_init(&m->send[k], m->sample_rate, KIWI_SMOOTH_MS, send[k]);
        }
        kiwi_smoother_set(&m->vol[k], vol[k]);
        kiwi_smoother_set(&m->send[k], send[k]);
    }
    m->primed = 1;

    /* Each sample reads every input before writing any output, so hosts may
     * share input and output buffers. */
    for (uint32_t i = 0; i < n; i++) {
        float dry_l = 0.0f, dry_r = 0.0f, send_l = 0.0f, send_r = 0.0f;
        for (int k = 0; k < KIWI_MODES; k++) {
            float v = kiwi_taper(kiwi_smoother_next(&m->vol[k]));
            float s = v * kiwi_taper(kiwi_smoother_next(&m->send[k]));
            float l = in[k * 2][i];
            float r = in[k * 2 + 1][i];
            dry_l += l * v;
            dry_r += r * v;
            send_l += l * s;
            send_r += r * s;
        }
        out[0][i] = dry_l;
        out[1][i] = dry_r;
        out[2][i] = send_l;
        out[3][i] = send_r;
    }
}

void kiwi_carrier_init(kiwi_carrier *c, float sample_rate) {
    kiwi_smoother_init(&c->blend, sample_rate, KIWI_SMOOTH_MS, 0.0f);
    c->sample_rate = sample_rate;
    c->primed = 0;
}

void kiwi_carrier_process(kiwi_carrier *c, const float **in, float *out,
                          float blend, uint32_t n) {
    if (!c->primed)
        kiwi_smoother_init(&c->blend, c->sample_rate, KIWI_SMOOTH_MS, blend);
    kiwi_smoother_set(&c->blend, blend);
    c->primed = 1;

    for (uint32_t i = 0; i < n; i++) {
        float gs, gp, gm;
        kiwi_carrier_gains(kiwi_smoother_next(&c->blend), &gs, &gp, &gm);
        float synth = 0.5f * (in[0][i] + in[1][i]);
        float piano = 0.5f * (in[2][i] + in[3][i]);
        float sampler = 0.5f * (in[4][i] + in[5][i]);
        out[i] = gs * synth + gp * piano + gm * sampler;
    }
}
