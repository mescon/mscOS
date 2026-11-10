/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * mscOS: native DSD for C-Media CM6632A receivers with a 64-bit raw subslot
 * (Mark Levinson No585/No585.5, 0ecb:18a7). See mscos_dsd.c.
 */
#ifndef __USBAUDIO_MSCOS_DSD_H
#define __USBAUDIO_MSCOS_DSD_H

#include <linux/types.h>

struct snd_usb_audio;
struct snd_usb_endpoint;
struct snd_usb_substream;
struct audioformat;

/* quirks.c: claim the raw-DSD altsetting; returns the ALSA format bits or 0 */
u64 mscos_dsd_format(struct snd_usb_audio *chip, struct audioformat *fp,
		     unsigned int sample_bytes);
/* stream.c: offer the DSD altsetting on the PCM device of the same interface */
bool mscos_dsd_join(struct snd_usb_substream *subs, const struct audioformat *fp);
/* format.c: rates in the table are the device clock; ALSA frames run twice as fast */
void mscos_dsd_fixup_rates(struct audioformat *fp);
/* clock.c: the rate to program into the device clock */
int mscos_dsd_clock_rate(const struct audioformat *fmt, int rate);
/* endpoint.c: keep every packet a whole number of device frames (2 ALSA frames) */
int mscos_dsd_packet_frames(struct snd_usb_endpoint *ep, int frames);
/* pcm.c: reorder L0 R0 L1 R1 into L0 L1 R0 R1, in place */
void mscos_dsd_pack(const struct audioformat *fmt, void *buf, unsigned int bytes);

#endif /* __USBAUDIO_MSCOS_DSD_H */
