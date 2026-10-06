CFLAGS ?= -O2 -std=gnu11 -Wno-unused-result
SRC = wspr_decode_iq.c wsprd.c wsprd_utils.c wsprsim_utils.c tab.c fano.c nhash.c kiss_fft.c

wspr_decode_iq: $(SRC)
	$(CC) $(CFLAGS) -I. -o $@ $(SRC) -lm

clean:
	rm -f wspr_decode_iq
