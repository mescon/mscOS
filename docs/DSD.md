# Rå DSD till Mark Levinson Nº585.5

Status 2026-11-10: **DSD64 fungerar** i full speed. Förstärkarens display visade "DSD" i testet, som kördes tyst med mute och volym Off. Ljudet är ännu inte avlyssnat. DSD128 och högre kräver USB high speed, se KB-artikeln.

## Varför en patch behövs

585.5:an har en USB-mottagare från C-Media, CM6632A (0ecb:18a7). Utöver tre PCM-lägen har den ett läge för rå DSD:

```
Interface 1, Altset 4: bmFormats 0x80000000 (RAW_DATA), bSubslotSize 8, bBitResolution 64,
EP 0x05 OUT async (feedback 0x85), wMaxPacketSize 800 (full speed)
```

- **Ingen DoP:** CM6632A avkodar inte DoP, så rå DSD är den enda vägen.
- **Linux kan inte driva läget:**
  - Linux stöd för rå DSD (`QUIRK_FLAG_DSD_RAW`) ger `DSD_U32_BE` med 4 byte per kanal och ram.
  - Enheten vill ha 8 byte per kanal, i en annan ordning och med klockan på halva takten.
  - Utan patch syns läget som `hw:0,1` med formatet SPECIAL, alltså oanvändbart.

## Varifrån formatet kommer

Mark Levinsons Mac-drivrutin ("USB Audio Driver Software for Mac OSX") innehåller `CmediaUSBDSD.kext` 2.7.16. Den binder uttryckligen till `HarmanUSBDSD 0ecb:18a7`, och även till Hegel HD12, Pioneer och andra CM6632A-enheter. `CmediaUSBDSDStream::PrepareDoPContent` gör om DoP från macOS till enhetens format. Disassemblerat:

- **Fyra ramar blir en:** 4 DoP-ramar (16 DSD-bitar per kanal och ram) blir 1 enhetsram.
- **Enhetsramen** består av 32-bitarsord i little-endian med den tidigaste biten i MSB:
  ```
  L(t0) L(t1) R(t0) R(t1)
  ```
- **Klockan** står på DoP-takten / 4, alltså DSD-takten / 64. DSD64 ger 44,1 kHz.
- **Tystnad** är 0x69, samma som i Linux.

Enhetsramen motsvarar ALSA:s `DSD_U32_LE` med två ALSA-ramar per enhetsram och mittorden bytta.

## Patchen (`kernel/mscos-dsd/`)

- **Logiken** ligger i en egen fil, `sound/usb/mscos_dsd.c`. Krokarna i befintliga filer är en rad var: quirks, format, clock, endpoint (två) och pcm (två).
- **Avgränsning:** patchen är aktiv bara för enheter i `mscos_dsd_ids[]` och bara på ett läge med RAW_DATA och 8 byte per subslot. Modulparametern `mscos_dsd=0` stänger av den.
- **Vad den gör:**
  - visar läget som stereo `DSD_U32_LE` och dubblar takttabellen, så att DSD64 blir 88 200 Hz;
  - sätter enhetens klocka på halva ALSA-takten;
  - håller varje USB-paket till ett jämnt antal ALSA-ramar och skjuter en eventuell överbliven ram till nästa paket. Lågfördröjningsläget är därför avstängt för det här läget;
  - byter plats på ord 1 och 2 i varje grupp om 16 byte när URB:en fylls.
- **Endpointen räknar i ALSA-enheter.** Paketstorlekar, perioder och asynkron feedback behöver alltså inga ändringar. Kärnan identifierar själv feedbackformatet (11.13 i stället för 10.14) genom att skifta värdet.

Upstream-ändringar i snd-usb-audio rör sällan just de här raderna. Patchen ska därför bara behöva läggas på igen och byggas om när kärnan byts.

## Bygga och installera

```sh
kernel/mscos-dsd/build-module.sh            # mot armbian-build/cache/sources/linux-kernel-worktree/6.18__sunxi__armhf
kernel/mscos-dsd/install-module.sh          # till /lib/modules/<release>/updates/
kernel/mscos-dsd/install-module.sh root@192.168.1.200 --remove   # tillbaka till originalet
```

- **Modulen:** den byggs i Armbians byggcontainer med samma gcc som kärnan, och kärnträdet monteras skrivskyddat.
- **Varför en ensam modul räcker:** kärnan saknar modulsignering och MODVERSIONS, så det enda som måste stämma är vermagic. Installationsskriptet kontrollerar den.
- **Reproducerbart:** bygget ger samma md5 varje gång.
- **Hel kärna:** för ett fullständigt kärnbygge ligger samma patch i `kernel/userpatches/kernel/archive/sunxi-6.18/9901-mscos-cm6632a-native-dsd.patch`.

## Testa

```sh
python3 tools/dsf2u32le.py fil.dsf /tmp/t.raw 40 20       # 20 s från 0:40, DSD64 stereo
# på enheten, i Roon-läge (PipeWire ska inte hålla DAC:n), förstärkaren tystad:
aplay -D hw:0,1 -t raw -f DSD_U32_LE -c 2 -r 88200 /tmp/t.raw
sed -n '/Status/,/Feedback/p' /proc/asound/card0/stream1    # Momentary freq ≈ 88200
```

Roon Bridge väljer `DSD_U32_LE` på `hw:0,1` som native DSD. Ställ in det i Roon under Settings → Audio → enheten → DSD Playback Strategy: Native.

## Säkerhet

Fel packning kan ge kraftigt brus i högtalarna. Det första lyssningsprovet görs därför med förstärkaren på låg volym. Panelen har ett volymtak på 40 dB, men detta går förbi panelen.
