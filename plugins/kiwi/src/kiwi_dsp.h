#ifndef KIWI_DSP_H
#define KIWI_DSP_H

#include <stdint.h>

#define KIWI_SMOOTH_MS 20.0f

enum { KIWI_PIANO = 0, KIWI_SAMPLER = 1, KIWI_VOCODER = 2, KIWI_MODES = 3 };

/* One-pole parameter smoother; snaps to the target once within 1e-6. */
typedef struct {
    float value;
    float target;
    float coef;
} kiwi_smoother;

void kiwi_smoother_init(kiwi_smoother *s, float sample_rate, float time_ms, float initial);
void kiwi_smoother_set(kiwi_smoother *s, float target);
float kiwi_smoother_next(kiwi_smoother *s);

/* Knob position 0..1 to gain: 0 is exact silence, 1 is unity. */
float kiwi_taper(float x);

/* Equal-power gains for blend 0..2: synth -> piano -> sampler. */
void kiwi_carrier_gains(float blend, float *synth, float *piano, float *sampler);

typedef struct {
    kiwi_smoother vol[KIWI_MODES];
    kiwi_smoother send[KIWI_MODES];
    float sample_rate;
    int primed;
} kiwi_mix;

void kiwi_mix_init(kiwi_mix *m, float sample_rate);
/* in: piano L/R, sampler L/R, vocoder L/R. out: dry L/R, send L/R.
 * vol and send hold one 0..1 knob value per mode. */
void kiwi_mix_process(kiwi_mix *m, const float **in, float **out,
                      const float *vol, const float *send, uint32_t n);

typedef struct {
    kiwi_smoother blend;
    float sample_rate;
    int primed;
} kiwi_carrier;

void kiwi_carrier_init(kiwi_carrier *c, float sample_rate);
/* in: synth L/R, piano L/R, sampler L/R. out: mono carrier. */
void kiwi_carrier_process(kiwi_carrier *c, const float **in, float *out,
                          float blend, uint32_t n);

#endif
