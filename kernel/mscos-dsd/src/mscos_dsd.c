// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * mscOS: native DSD for C-Media CM6632A receivers with a 64-bit raw subslot.
 *
 * The Mark Levinson No585/No585.5 (Harman, 0ecb:18a7) uses a C-Media CM6632A
 * USB receiver. Besides three PCM altsettings it offers a raw-DSD one:
 *
 *   bmFormats 0x80000000 (TYPE_I_RAW_DATA), bSubslotSize 8, bBitResolution 64
 *
 * The CM6632A does not decode DoP. C-Media's own macOS driver
 * (CmediaUSBDSD.kext 2.7.16, CmediaUSBDSDStream::PrepareDoPContent, which binds
 * to "HarmanUSBDSD" 0ecb:18a7) shows the wire format: one device frame carries
 * 64 DSD bits per channel, as two 32-bit little-endian words with the earliest
 * bit in the MSB, left channel first:
 *
 *   L(t0) L(t1) R(t0) R(t1)        (each a u32 LE, t1 = the next 32 bits)
 *
 * and the device clock runs at DSD rate / 64 (DSD64 = 44.1 kHz).
 *
 * That is ALSA's DSD_U32_LE with two frames per device frame and the middle
 * words swapped. So userspace sees an ordinary stereo DSD_U32_LE device at the
 * usual rates (DSD64 = 88200), and the driver:
 *   - doubles the rate table (the descriptors report the device clock),
 *   - programs the device clock at half the ALSA rate,
 *   - keeps every USB packet a whole device frame (an even number of ALSA
 *     frames; the remainder is carried to the next packet),
 *   - swaps words 1 and 2 of every 16 bytes when the URB is filled.
 * The endpoint stays in ALSA units, so packet sizes, periods and the asynchronous
 * feedback (whose format the driver already detects by shifting) need no change.
 *
 * Everything is limited to the devices in mscos_dsd_ids[] and to an altsetting
 * that is raw DSD with an 8-byte subslot; mscos_dsd=0 turns it off.
 */

#include <linux/module.h>
#include <linux/usb.h>
#include <sound/core.h>
#include <sound/pcm.h>

#include "usbaudio.h"
#include "card.h"
#include "mscos_dsd.h"

static bool mscos_dsd = true;
module_param(mscos_dsd, bool, 0444);
MODULE_PARM_DESC(mscos_dsd, "mscOS: native DSD for CM6632A 64-bit raw subslot devices (default: yes).");

static const u32 mscos_dsd_ids[] = {
	USB_ID(0x0ecb, 0x18a7),	/* Harman: Mark Levinson No585 / No585.5 */
};

static bool mscos_dsd_known(u32 id)
{
	int i;

	for (i = 0; i < ARRAY_SIZE(mscos_dsd_ids); i++)
		if (mscos_dsd_ids[i] == id)
			return true;
	return false;
}

u64 mscos_dsd_format(struct snd_usb_audio *chip, struct audioformat *fp,
		     unsigned int sample_bytes)
{
	if (!mscos_dsd || !fp->dsd_raw || sample_bytes != 8 || fp->channels != 2 ||
	    !mscos_dsd_known(chip->usb_id))
		return 0;
	fp->mscos_dsd64 = true;
	usb_audio_info(chip, "%u:%d: mscOS native DSD (64-bit subslot as DSD_U32_LE)\n",
		       fp->iface, fp->altsetting);
	return SNDRV_PCM_FMTBIT_DSD_U32_LE;
}

void mscos_dsd_fixup_rates(struct audioformat *fp)
{
	unsigned int i;

	if (!fp->mscos_dsd64)
		return;
	fp->rates = 0;
	for (i = 0; i < fp->nr_rates; i++) {
		fp->rate_table[i] *= 2;
		fp->rates |= snd_pcm_rate_to_rate_bit(fp->rate_table[i]);
	}
	fp->rate_min *= 2;
	fp->rate_max *= 2;
}

int mscos_dsd_clock_rate(const struct audioformat *fmt, int rate)
{
	return (fmt && fmt->mscos_dsd64) ? rate / 2 : rate;
}

int mscos_dsd_packet_frames(struct snd_usb_endpoint *ep, int frames)
{
	unsigned int total;

	if (frames <= 0 || !ep->cur_audiofmt || !ep->cur_audiofmt->mscos_dsd64)
		return frames;
	total = frames + ep->mscos_carry;
	ep->mscos_carry = total & 1;
	return total & ~1u;
}

void mscos_dsd_pack(const struct audioformat *fmt, void *buf, unsigned int bytes)
{
	u32 *w = buf, t;
	unsigned int n;

	if (!fmt || !fmt->mscos_dsd64)
		return;
	for (n = bytes / 16; n; n--, w += 4) {
		t = w[1];
		w[1] = w[2];
		w[2] = t;
	}
}
