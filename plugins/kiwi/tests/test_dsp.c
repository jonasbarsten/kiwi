#include <math.h>
#include <stdio.h>
#include "kiwi_dsp.h"

#define SR 48000.0f
#define N 256

static int failures = 0;

#define CHECK(cond, ...) do { \
    if (!(cond)) { failures++; printf("FAIL %s:%d: ", __func__, __LINE__); \
                   printf(__VA_ARGS__); printf("\n"); } \
} while (0)

static void fill(float *buf, float v) { for (int i = 0; i < N; i++) buf[i] = v; }

static void test_taper(void) {
    CHECK(kiwi_taper(0.0f) == 0.0f, "taper(0) = %f", kiwi_taper(0.0f));
    CHECK(kiwi_taper(1.0f) == 1.0f, "taper(1) = %f", kiwi_taper(1.0f));
    CHECK(kiwi_taper(-0.5f) == 0.0f, "taper clamps below");
    CHECK(kiwi_taper(2.0f) == 1.0f, "taper clamps above");
    float prev = 0.0f;
    for (int i = 1; i <= 100; i++) {
        float g = kiwi_taper(i / 100.0f);
        CHECK(g > prev, "taper not increasing at %d", i);
        prev = g;
    }
}

static void test_carrier_endpoints(void) {
    float s, p, m;
    kiwi_carrier_gains(0.0f, &s, &p, &m);
    CHECK(s == 1.0f && p == 0.0f && m == 0.0f, "blend 0 -> %f %f %f", s, p, m);
    kiwi_carrier_gains(1.0f, &s, &p, &m);
    CHECK(s == 0.0f && p == 1.0f && m == 0.0f, "blend 1 -> %f %f %f", s, p, m);
    kiwi_carrier_gains(2.0f, &s, &p, &m);
    CHECK(s == 0.0f && p == 0.0f && m == 1.0f, "blend 2 -> %f %f %f", s, p, m);
    kiwi_carrier_gains(-1.0f, &s, &p, &m);
    CHECK(s == 1.0f && p == 0.0f && m == 0.0f, "blend clamps below");
    kiwi_carrier_gains(3.0f, &s, &p, &m);
    CHECK(s == 0.0f && p == 0.0f && m == 1.0f, "blend clamps above");
}

static void test_carrier_equal_power(void) {
    for (int i = 0; i <= 200; i++) {
        float s, p, m;
        kiwi_carrier_gains(i / 100.0f, &s, &p, &m);
        float power = s * s + p * p + m * m;
        CHECK(fabsf(power - 1.0f) < 1e-5f, "power %f at blend %f", power, i / 100.0f);
    }
}

static void test_smoother_converges_exactly(void) {
    kiwi_smoother s;
    kiwi_smoother_init(&s, SR, KIWI_SMOOTH_MS, 0.0f);
    CHECK(kiwi_smoother_next(&s) == 0.0f, "initial value kept");
    kiwi_smoother_set(&s, 1.0f);
    float v = 0.0f;
    for (int i = 0; i < (int)SR; i++) v = kiwi_smoother_next(&s);
    CHECK(v == 1.0f, "after 1 s value = %.9f", v);
}

static void run_mix(kiwi_mix *m, const float *vol, const float *send, float in_value,
                    float *dl, float *dr, float *sl, float *sr) {
    static float in_buf[6][N];
    const float *in[6];
    float *out[4] = { dl, dr, sl, sr };
    for (int c = 0; c < 6; c++) { fill(in_buf[c], in_value); in[c] = in_buf[c]; }
    kiwi_mix_process(m, in, out, vol, send, N);
}

static void test_mix_volume_zero_is_silent(void) {
    kiwi_mix m;
    kiwi_mix_init(&m, SR);
    float vol[3] = { 0, 0, 0 }, send[3] = { 1, 1, 1 };
    float dl[N], dr[N], sl[N], sr[N];
    run_mix(&m, vol, send, 1.0f, dl, dr, sl, sr);
    for (int i = 0; i < N; i++)
        CHECK(dl[i] == 0.0f && dr[i] == 0.0f && sl[i] == 0.0f && sr[i] == 0.0f,
              "sample %d not silent", i);
}

static void test_mix_post_fader(void) {
    kiwi_mix m;
    kiwi_mix_init(&m, SR);
    float vol[3] = { 1, 0, 0 }, send[3] = { 1, 1, 1 };
    float dl[N], dr[N], sl[N], sr[N];
    run_mix(&m, vol, send, 0.5f, dl, dr, sl, sr);
    /* Only piano passes: dry = send = input, sampler and vocoder add nothing. */
    CHECK(dl[N - 1] == 0.5f && dr[N - 1] == 0.5f, "dry %f %f", dl[N - 1], dr[N - 1]);
    CHECK(sl[N - 1] == 0.5f && sr[N - 1] == 0.5f, "send %f %f", sl[N - 1], sr[N - 1]);
}

static void test_mix_send_zero(void) {
    kiwi_mix m;
    kiwi_mix_init(&m, SR);
    float vol[3] = { 1, 1, 1 }, send[3] = { 0, 0, 0 };
    float dl[N], dr[N], sl[N], sr[N];
    run_mix(&m, vol, send, 0.25f, dl, dr, sl, sr);
    CHECK(dl[N - 1] == 0.75f, "dry sums three modes: %f", dl[N - 1]);
    CHECK(sl[N - 1] == 0.0f && sr[N - 1] == 0.0f, "send %f %f", sl[N - 1], sr[N - 1]);
}

static void test_mix_jump_is_smoothed(void) {
    kiwi_mix m;
    kiwi_mix_init(&m, SR);
    float vol[3] = { 0, 0, 0 }, send[3] = { 0, 0, 0 };
    float dl[N], dr[N], sl[N], sr[N];
    run_mix(&m, vol, send, 1.0f, dl, dr, sl, sr);
    vol[KIWI_PIANO] = 1.0f;
    run_mix(&m, vol, send, 1.0f, dl, dr, sl, sr);
    CHECK(dl[0] < 0.001f, "first sample after jump = %f", dl[0]);
    CHECK(dl[N - 1] > dl[0], "gain rising: %f -> %f", dl[0], dl[N - 1]);
}

static void test_carrier_process(void) {
    kiwi_carrier c;
    kiwi_carrier_init(&c, SR);
    static float synth_l[N], synth_r[N], piano_l[N], piano_r[N], samp_l[N], samp_r[N];
    fill(synth_l, 1.0f); fill(synth_r, 0.5f);
    fill(piano_l, 0.2f); fill(piano_r, 0.2f);
    fill(samp_l, -1.0f); fill(samp_r, -1.0f);
    const float *in[6] = { synth_l, synth_r, piano_l, piano_r, samp_l, samp_r };
    float out[N];
    kiwi_carrier_process(&c, in, out, 0.0f, N);
    CHECK(out[N - 1] == 0.75f, "blend 0 = synth mono: %f", out[N - 1]);

    kiwi_carrier_init(&c, SR);
    kiwi_carrier_process(&c, in, out, 2.0f, N);
    CHECK(out[N - 1] == -1.0f, "blend 2 = sampler mono: %f", out[N - 1]);
}

static void test_silence_stays_clean(void) {
    kiwi_mix m;
    kiwi_mix_init(&m, SR);
    float vol[3] = { 0.7f, 0.3f, 1 }, send[3] = { 0.2f, 0.9f, 0.5f };
    float dl[N], dr[N], sl[N], sr[N];
    for (int b = 0; b < 100; b++) run_mix(&m, vol, send, 0.0f, dl, dr, sl, sr);
    for (int i = 0; i < N; i++)
        CHECK(dl[i] == 0.0f && sl[i] == 0.0f && !isnan(dr[i]) && !isnan(sr[i]),
              "silence in, sample %d out %g", i, dl[i]);
}

int main(void) {
    test_taper();
    test_carrier_endpoints();
    test_carrier_equal_power();
    test_smoother_converges_exactly();
    test_mix_volume_zero_is_silent();
    test_mix_post_fader();
    test_mix_send_zero();
    test_mix_jump_is_smoothed();
    test_carrier_process();
    test_silence_stays_clean();
    if (failures) { printf("%d failure(s)\n", failures); return 1; }
    printf("all tests passed\n");
    return 0;
}
