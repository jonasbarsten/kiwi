CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c99
BUILD := build
BUNDLE := $(BUILD)/kiwi.lv2
LV2_DIR ?= $(HOME)/.lv2

.PHONY: all test install clean

all: $(BUNDLE)/kiwi.so

$(BUNDLE)/kiwi.so: src/kiwi_dsp.c src/kiwi_lv2.c src/kiwi_dsp.h ttl/manifest.ttl ttl/kiwi.ttl
	mkdir -p $(BUNDLE)
	$(CC) $(CFLAGS) -fPIC -shared $(shell pkg-config --cflags lv2) -o $@ src/kiwi_dsp.c src/kiwi_lv2.c -lm
	cp ttl/manifest.ttl ttl/kiwi.ttl $(BUNDLE)/

test: $(BUILD)/test_dsp
	./$(BUILD)/test_dsp

$(BUILD)/test_dsp: tests/test_dsp.c src/kiwi_dsp.c src/kiwi_dsp.h
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -Isrc -o $@ tests/test_dsp.c src/kiwi_dsp.c -lm

install: all
	mkdir -p $(LV2_DIR)
	rm -rf $(LV2_DIR)/kiwi.lv2
	cp -r $(BUNDLE) $(LV2_DIR)/

clean:
	rm -rf $(BUILD)
