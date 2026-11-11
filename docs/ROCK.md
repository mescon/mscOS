# Roon Optimized Core Kit (ROCK) jämfört med mscOS

Analys från 2026-11-10 av den officiella installern `roonbox-linuxx64-nuc4-usb-factoryreset.img.gz`: RoonOS 2.1 build 271, kärna 6.6.33 och RoonServer 2.0 build 1303.

- Avbildningen packades upp utan att något kördes.
- Kärnkonfigen lästes ur IKCONFIG.
- `quirk_flags_table` i snd-usb-audio jämfördes post för post med mainline 6.6.33.

**Slutsats:** ROCK är en generisk x86-avbildning (busybox och runit, read-only squashfs med tmpfs-overlay). Den gör nästan ingen ljudtrimning:
- ingen IRQ- eller CPU-uppdelning
- ingen RT-konfiguration utöver det RAAT (`libraatmanager.so`) själv begär med `sched_setscheduler`
- snd-usb-audio är identisk med mainline, utan egna DSD-quirks

DSD och DoP sköts i RAAT i user space. mscOS gör redan mer på alla punkter som går att tillämpa här.

| ROCK | mscOS | Bedömning |
|---|---|---|
| PREEMPT_DYNAMIC, HZ_1000 | Full PREEMPT, HZ_1000 | Redan gjort |
| cmdline `loglevel=0 consoleblank=0`, ingen affinitet | `irqaffinity=0 audit=0 rt_group_sched=0` | Redan gjort, och mer |
| DEBUG_KERNEL, SLUB_DEBUG, KPROBES, FTRACE och AUDIT påslagna | Avstängda | Redan gjort |
| Governor `ondemand` (fungerar inte med intel_pstate) | `performance` på 960 MHz | Redan gjort |
| Ingen IRQ-affinitet, RPS eller workqueue-mask | USB på CPU1, allt annat på CPU0 | Redan gjort |
| RoonServer utan nice, RT eller affinitet. RAAT begär själv RT | Roon Bridge `CPUAffinity=0 1` | **Kontrollera** att RAAT får FIFO som root |
| Read-only squashfs med tmpfs-overlay | overlayroot valbart, cache i tmpfs | Redan gjort |
| A/B-rotavbildningar, ingen watchdog | Watchdog och säkerhetskopia | Ej tillämpligt |
| snd-usb-audio som i mainline | Egen DSD-patch för CM6632A | Inget att hämta från ROCK |
| .NET: Server GC, TieredCompilation (Core på x86) | Egna `DOTNET_*` för A20 | Ej tillämpligt |

## Infört i mscOS efter jämförelsen (2026-11-10)

1. **RAATServer på ljudkärnan:** `sources/roon/raat-cpu` och `raat-cpu.service` flyttar RAATServer till CPU1 när port 9004 svarar. RoonBridge startar ibland om RAATServer, och den nya processen fångas upp vid nästa koll. Det är testat: affinitetsmask 2, 18 trådar.
2. **Konsolnivå 0:** `verbosity=0` i `armbianEnv.txt`, så kärnan startar med `loglevel=0`. Det börjar gälla vid nästa omstart.
3. **RT för RAAT:** RAAT begär det själv, precis som i ROCK. Kontrollera vid första uppspelningen i Roon med `ps -eLo tid,cls,rtprio,psr,comm -p $(pgrep -x RAATServer)` och genom att leta efter `sched_setscheduler` i `/var/roon/RAATServer/Logs/`. Saknas FIFO, gör en motsvarighet till `soloist-rt`.
