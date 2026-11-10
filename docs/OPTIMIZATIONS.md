# Optimeringar i mscOS

Det här gör mscOS för bitperfekt uppspelning utan avbrott på sMS-200Ultra (Allwinner A20: två Cortex-A7, 2 GB RAM). Varje punkt anger var den finns och varför.

## Kärna (`kernel/userpatches/extensions/sotm-audio.sh`)

| Inställning | Varför |
|---|---|
| `PREEMPT`, `HZ_1000` | Kort väntan från USB-avbrott till ljudtråden |
| FTRACE, KPROBES, PSI, AUTOGROUP och debug-alternativ av | Ingen instrumentering i varmvägen |
| `rt_group_sched=0 audit=0` vid start | RT_GROUP_SCHED och AUDIT kan inte stängas av i konfigen (AppArmor och Armbian-hookar), så de stängs av vid start |
| `irqaffinity=0` | Alla avbrott börjar på CPU0, och ljudet flyttas sedan till CPU1 |
| `loglevel=0` (`verbosity=0`) | Ingen konsolutskrift från kärnan, som i ROCK |
| `mscos-dsd` | Rå DSD till CM6632A, se [DSD.md](DSD.md) |

## CPU-delning och avbrott (`device/tune-system.sh`, `device/sotm-tuning`)

- **Klockfrekvens:** governor `performance`, fast 960 MHz.
- **CPU0** tar allt annat: nätverkets IRQ och RPS, SD-kortet, panelen, sshd och obundna workqueues.
- **CPU1** är ljudkärnan. Där körs avbrotten från USB-styrkretsarna (EHCI/OHCI) och, via `user@<uid>.service.d CPUAffinity=1`, ljudanvändarens tjänster PipeWire, WirePlumber och Soloist.
- **Soloists ljudtrådar** (`pipewire-loop`, `Audio Driver Th`, `Media Mixer Ren`) blir SCHED_FIFO 70 via `soloist-rt`. Annars missar de PipeWires deadline.
- **Roon Bridge** kör `CPUAffinity=0 1`, eftersom .NET-starten behöver båda kärnorna. Annars startar RoonBridge om RAATServer i en loop.
- **RAATServer** flyttas till CPU1 av `raat-cpu` (bunden till roonbridge.service) så fort port 9004 svarar. Hela processen flyttas, och nya trådar ärver CPU1. RAAT sätter själv SCHED_FIFO på sina ljudtrådar, och som root lyckas det.

## Hårdvara som stängs av (`device/sotm-lean.dts`)

Enheten kör Cubietrucks device tree. En DT-overlay stänger av LCD, HDMI, GPU och video-avkodaren, SATA, WiFi-SDIO, krypto, PWM, S/PDIF, IR, den analoga codecen, Bluetooth-UART:en och **GPADC**.

Termosensorn i GPADC saknar fungerande IRQ på kortet, så varje avläsning blockerar kärnan i cirka 110 ms. Det gav 3–4 avbrott i ljudet varje sekund. Temperaturen läses i stället från AXP209.

## Ljudkedjan (`device/10-bitperfect.conf`, `device/50-usb-dac.conf`)

- **Fast graf:** PipeWire står på 44,1 kHz, quantum 1024, utan omsampling, dither eller kanalmixning.
- **Rå hw-enhet:** DAC:n körs i pro-audio-profilen, med S32LE så att 24 bitar går igenom orörda.
- **Ingen suspend:** DAC:n hålls öppen hela tiden, så att den inte klickar när uppspelningen startar.
- **Oanvända ljudkort** (A20-codecen och HDMI) är avstängda.
- **Spotify-reglaget styr 585.5:an** (`web/backend/volbridge.py`).
  - Soloist sänker volymen genom att sätta `channelVolumes` på sin PipeWire-ström, och läser sedan tillbaka värdet.
  - Panelen sätter därför `softVolumes` till 1,0 efter varje ändring. Samplen går då orörda till DAC:n, och Soloist märker ingenting.
  - Reglaget flyttas till förstärkaren: 0–100 % motsvarar 0–40 dB, med högst +6 dB per ändring.
  - Ändringar med fjärrkontrollen, i HA eller i panelen flyttar reglaget tillbaka.
  - Verifierat: bitperfekt 24 bitar och 0,0 dB med Spotify på 50 %.
- **Bitperfekt verifieras** i panelen. Panelen spelar in sinkens monitor och kontrollerar att samplen ligger på 16-bitarsgittret med 0,0 dB förstärkning.

## Brus och schemaläggning

- **Mindre i bakgrunden:** apt-daily, man-db, e2scrub, dpkg-db-backup, rsyslog och getty är avstängda. fstrim och logrotate körs klockan 04:40–04:45.
- **Ethernet på 100 Mbit:** det drar 0,12 W mindre på ACIN-rälsen än 1000 Mbit. Panelen kan ändra det.
- **Soloists cache** ligger i tmpfs (400 MB), så att uppspelningen inte skriver till SD-kortet.
- **Lysdioderna** kan stängas av från panelen.

## Robusthet

- **Watchdog:** maskinvaruwatchdogen har `RuntimeWatchdogSec=15s` (sunxi-wdt klarar högst 16 s).
- **Fast kärna:** kärnpaketen är spärrade (`apt-mark hold`).
- **Återställning:** det finns en säkerhetskopia av rotfilsystemet och början av SD-kortet (U-Boot och MBR) i `backup/`, med RESTORE.md.
- **Skrivskyddat rotfilsystem:** overlayroot går att välja i panelen.

## Utvärderat och inte infört

- **Allt från ROCK:** Roon Optimized Core Kit gör nästan ingen ljudtrimning, se [ROCK.md](ROCK.md).
