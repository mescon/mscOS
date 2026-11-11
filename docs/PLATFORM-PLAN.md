# mscOS som distribution: plattformar, bas och plan

Status 2026-11-11: förslag, inget av detta är byggt ännu. Målet är ett musik-OS som andra hifi-entusiaster kan flasha på sin egen enhet och ställa in själva:

- med sina egna nycklar och konton, inga av våra,
- med valfri DAC, där Mark Levinson och Arcam bara är tillval,
- med ett gränssnitt på svenska och engelska.

## 1. Utgångsläget

Det mesta i mscOS är redan generellt Linux. Det som är bundet till just den här installationen är:

| Bundet till oss | Var | Vad som behövs |
|---|---|---|
| Adresser och värdnamn: panelens publika adress, IP-adresser, Arcam-IP, betrodd proxy | `app.py`, `sotm-web.service`, `deploy.sh`, verktygen | inställningar som sätts vid första start, inga standardvärden från vårt nät |
| Panelen kräver omvänd proxy (`only_via_proxy`) | `app.py` | lokalt läge som standard (`http://mscos.local`, mDNS), proxyläget som tillval |
| Mark Levinson 585.5 och Arcam AVR10 är inbyggda | `levinson.py`, `arcam.py`, `volbridge.py`, vyerna | förstärkardrivrutiner som tillägg; utan förstärkare styr reglaget DAC:ns egen volym eller inget alls |
| Kortspecifik trimning: A20, AXP209, IRQ-namn, lysdioder | `tune-system.sh`, `sotm-tuning`, `sotm-lean.dts`, `collect.py` | en kortprofil per enhet |
| Svenska i koden: cirka 120 strängar i `app.js`, 70 i `index.html`, 90 i backend | överallt | översättningsfiler och meddelandenycklar från backend |
| CM6632A-patchen byggs mot vårt kärnträd | `kernel/mscos-dsd/` | DKMS för distributionen, på sikt upstream i Linux |
| Soloist-nyckeln | `/etc/soloist.env` | användaren skapar sin egen nyckel i guiden vid första start; Spotify kräver det |

## 2. Enheter

| Enhet | SoC | Bas | Kommentar |
|---|---|---|---|
| **SOtM sMS-200, sMS-200ultra** | Allwinner A20 (armv7, 2 kärnor) | **Armbian** sunxi, nuvarande kärna 6.18 | det vi kör nu; Eunhasu ersätts helt |
| SOtM sMS-200 Neo, sMS-200ultra Neo, SE | troligen samma A20-kort | Armbian sunxi | **behöver verifieras** med en enhet: SOtM anger inte SoC för Neo och SE ([hifiplus](https://hifiplus.com/articles/sotm-sms-200-ultra-neo-se-tx-usb-ultra-se-and-sps-500-se-streaming-system/), [audiobacon](https://audiobacon.net/?p=11802)) |
| SOtM sMS-1000SQ, sMS-2000 | x86 | Debian, eller Armbian uefi-x86 | Eunhasu på x86 ([SOtM](https://www.sotm-audio.com/sotmwp/english/portfolio-item/sms-2000/)) |
| **Raspberry Pi 4, 5, CM4, CM5** | Broadcom, arm64 | Armbian rpi4b (Raspberry Pi-kärnan), eller Raspberry Pi OS Lite | störst användarbas; I2S-kort (HiFiBerry, Allo, IQaudio) kräver Pi-kärnans overlays |
| Allo USBridge Signature | Raspberry Pi CM3+ | Armbian rpi, eller Raspberry Pi OS | i praktiken en Pi |
| Odroid C4, N2+ | Amlogic S905X3, S922X | Armbian meson | |
| Radxa Rock 4/5, Orange Pi 5, NanoPi R5S/R6S | Rockchip RK3399, RK3588, RK3568 | Armbian rockchip64 | kraftiga nog för HQPlayer Embedded och uppsampling till DSD |
| Mini-PC: Intel N100, NUC | x86-64 | Armbian uefi-x86, eller Debian | samma användning som ROCK; kan själv köra HQPlayer |
| Sonore Rendu, Innuos, Lumin och liknande | egna kort | – | slutna system; ingen väg in utan tillverkaren |

**Slutsats om basen:** **Armbians byggsystem** täcker alla enheterna i listan: sunxi, rpi4b, meson, rockchip64 samt uefi-x86 och uefi-arm64. Användarlandet blir Debian 13 (Trixie) överallt.
- mscOS blir ett lager ovanpå, byggt med `userpatches/customize-image.sh` som vi redan använder i `prepare-armbian-image.sh`.
- Allt mscOS-specifikt blir samma Debianpaket på alla kort. Bara kärnan och bootloadern skiljer.
- Raspberry Pi OS behövs bara om Armbians Pi-bygge saknar något overlay för ett I2S-kort. Paketen fungerar på båda.

## 3. Arkitektur

```
┌─ Panel (PWA) ── vyer som ES-moduler, laddas efter /api/capabilities; sv/en ─┐
├─ mscos-panel ── kärna + tillägg ─────────────────────────────────────────────┤
│  källor/renderare:  spotify (Soloist) · roon · hqplayer-naa · upnp · squeezelite · airplay
│  förstärkare:       levinson-585 · arcam-fmj · ingen (DAC-volym/ingen volym)
│  DAC-tillägg:       cm6632a-dsd · generisk UAC2
├─ mscos-core ── PipeWire bitperfekt, DAC-ägare (en källa åt gången), kortprofil, CPU-delning
├─ Kortlager ── Armbian-kärna och dts per kort; DKMS-modul för CM6632A
└─ Debian 13
```

- **Distributionen är Debianpaket i ett apt-arkiv**: `mscos-core`, `mscos-panel`, `mscos-source-*`, `mscos-amp-*` och `mscos-dsd-dkms`. Avbildningen är Armbian plus paketen plus en guide vid första start.
  - Uppdateringar sker med apt, som en knapp i panelen.
  - Avbildningen behöver inte byggas om för varje ändring.
- **Ägare av DAC:n:** källväxeln mellan Spotify och Roon blir generell. En källa åt gången äger DAC:n och de andra stoppas, som i dag.
  - Soloist går via PipeWire.
  - Roon, NAA, MPD och Squeezelite kan öppna `hw:` direkt eller gå via PipeWire, beroende på vad som ger bitperfekt med minst belastning.
- **Kortprofil** i `/usr/share/mscos/boards/<kort>.yaml`. Den beskriver:
  - CPU-delning (ljudkärna, IRQ-namn för USB),
  - lysdioder,
  - sensorer (A20:s AXP209, Pi:s `vcgencmd`, hwmon),
  - nätverkets hastighetsval.

  `tune-system.sh` och `sotm-tuning` läser profilen i stället för att ha A20 inbyggt.
- **CM6632A-patchen:** skickas till alsa-devel för upstream, eftersom den bara rör en quirk-tabell och en egen fil. Tills den är inne byggs den som DKMS-modul per kärnserie.

## 4. Källor och tjänster: vad som går att distribuera

| Tjänst | Hur | Får vi distribuera? |
|---|---|---|
| **Spotify Lossless** | Soloist (Spotify Connect) | Nyckeln är personlig: den får inte delas, publiceras eller ingå i distribuerade skript ([Spotify](https://developer.spotify.com/documentation/soloist/concepts/authentication)). mscOS levereras därför helt utan nyckel. Spotify tillhandahåller Soloist för arm32, arm64 och x86-64 (kontrollerat 2026-11-11). Varje användare måste skapa en egen nyckel med ett Premium-konto, och binären får inte distribueras vidare ([Spotify: Authentication](https://developer.spotify.com/documentation/soloist/concepts/authentication), [Getting started](https://developer.spotify.com/documentation/soloist/tutorials/getting-started), [ioBroker](https://github.com/ioBroker/ioBroker.spotify-connect)). **Det fungerar redan så hos oss:** `soloist-update` hämtar från Spotifys server på enheten. Guiden länkar till nyckelsidan. Enligt Music Assistant säger Spotifys villkor inte tydligt att tredjepartsprogram får användas, så användningen sker på egen risk ([Music Assistant](https://www.music-assistant.io/music-providers/spotify/)). |
| Spotify utan nyckel | librespot (eller go-librespot) | öppen källkod, inloggning via Spotify-appen (Zeroconf), ingen nyckel. **Bara 320 kbit/s Ogg Vorbis**: Spotify skickar inte Lossless över Connect-protokollet till annat än certifierad hårdvara ([librespot #1580](https://github.com/librespot-org/librespot/issues/1580)). Återskapat protokoll, inte godkänt av Spotify. Volumio har fått höra av Spotify att de inte ska rota i DRM:en ([Volumio](https://community.volumio.com/t/new-2023-spotify-plugin/63381/600)). Standard i moOde och Volumio. Förslag: **standard i mscOS**, med Soloist som uppgradering till Lossless. |
| Spotify i fabriksbyggda spelare | Spotify Connect eSDK | bara för certifierade tillverkare: ansökan, NDA, certifieringstester och testenheter till Spotify ([Spotify: commercial hardware](https://developer.spotify.com/documentation/commercial-hardware/onboarding)). Tillverkaren har en nyckel per produkt, och användaren loggar in via appen (Zeroconf). Ingen väg för en fri distribution. |
| **Roon** | Roon Bridge | Roons installationsskript hämtas på enheten, som i dag. Qobuz och Tidal fungerar då via Roon. |
| **HQPlayer** | NAA (`networkaudiod`) | Signalyst distribuerar NAA för armhf, arm64 och amd64 ([AUR](https://aur.archlinux.org/packages/hqplayer-network-audio-daemon)). Hämtas på enheten. Licenstexten ska läsas innan något paketeras. HQPlayer Embedded (betalt) kan erbjudas på Rockchip- och x86-kort. Med HQPlayer fungerar Qobuz och Tidal via HQPlayer, inklusive uppsampling till DSD. |
| **Qobuz, Tidal via UPnP/OpenHome** | `upmpdcli` + MPD | öppen källkod. Styrs med BubbleUPnP, mConnect eller Linn Kazoo med användarens egna konton. Bitperfekt och hi-res. |
| **Qobuz, Tidal via Music Assistant/LMS** | Squeezelite | öppen källkod. Music Assistant (som redan finns hos dig) har Qobuz och Tidal. |
| Qobuz Connect | officiellt bara för certifierade tillverkare ([What Hi-Fi](https://www.whathifi.com/streaming-entertainment/music-streaming/qobuz-connect-officially-launches-with-wide-support-across-hi-fi-brands)) | moOde och Volumio har det ([moOde](https://moodeaudio.org/), [Volumio](https://community.volumio.com/t/qobuz-connect-integration-on-volumio-streamers/72037)). Det fristående `qbzd` är oberoende och ännu inte komplett ([qbz](https://github.com/b0bbywan/qbz)). Ett möjligt **tillval som användaren själv installerar**; ingår inte i grundavbildningen. |
| Tidal Connect | bara för tillverkare | de DIY-varianter som finns bygger på en tillverkares binär ([ifi-tidal-release](https://github.com/seniorgod/ifi-tidal-release)), och Tidal har ingen öppen väg ([GioF71](https://github.com/GioF71/tidal-connect)). **Tas inte med.** Tidal går via UPnP, Roon, HQPlayer eller Music Assistant. |
| AirPlay 2 | `shairport-sync` | öppen källkod; 44,1 kHz, ingen hi-res. |

**Format:** PCM upp till DAC:ns gräns. DSD som nativt DSD där DAC:n och drivrutinen klarar det (CM6632A med vår patch, och de kända DAC:arna i Linux quirk-tabell), annars som DoP. MQA stöds inte.

## 5. Panelen: modulär och översättningsbar

- **Översättning:** `static/i18n/sv.json` och `en.json`.
  - HTML-text märks med `data-i18n`, och JS använder `t("nyckel", {värden})`.
  - Backend skickar meddelandekoder med parametrar i stället för svensk text, till exempel `{"code": "amp.bypass", "args": {...}}`.
  - Språket följer webbläsaren och kan ändras under Utseende.
  - Talformat och datum skrivs med `Intl` (44,1 och 44.1).
- **Moduler:** `app.js` (935 rader) delas upp i ES-moduler, utan byggsteg eftersom A20 ska serva dem direkt:
  - `core/`: tillstånd, ws, i18n, tema,
  - `views/`: Lyssna, Signal, System,
  - `features/`: spotify, roon, naa, levinson, arcam, volbridge, kvalitet.
- **Funktionslista från backend:** `GET /api/capabilities` säger vilka källor, förstärkare och DAC-tillägg som är aktiva, och bara de vyerna laddas. Utan förstärkare syns ingen förstärkarvy, och utan Arcam ingen TV-del.
- **Backend:** `app.py` (1 550 rader) delas upp likadant:
  - `sources/`, med ett gemensamt gränssnitt: start, stopp, status, äger DAC:n,
  - `amps/`, med gränssnittet volym, ingång, standby och mute,
  - `boards/`.
- **Första start:** en guide som går igenom
  1. språk,
  2. panelens lösenord,
  3. nätverksnamn,
  4. DAC (identifieras automatiskt; visar format och om DSD stöds),
  5. källor (Spotify med användarens egen nyckel, Roon, HQPlayer NAA, UPnP med flera),
  6. förstärkare (ingen, Mark Levinson, Arcam och så vidare).

## 6. Ordning

1. **Städa.** Ta bort personliga och nätspecifika värden. Lägg in lokalt läge utan proxy och inställningar via guiden. Välj licens. *Sedan kan repot bli offentligt.*
2. **Översättning sv/en och modulär panel och backend.** Läge för generisk DAC utan förstärkare.
3. **Paket och apt-arkiv**, guiden vid första start.
4. **Avbildningar** med GitHub Actions och Armbians byggsystem:
   1. SOtM (A20), som vi kan testa,
   2. Raspberry Pi 4/5,
   3. x86 UEFI.

   Installationsanvisning: flasha med Raspberry Pi Imager eller balenaEtcher, starta och öppna `http://mscos.local`.
5. **Fler källor:** HQPlayer NAA, UPnP/OpenHome, Squeezelite, AirPlay 2.
6. **CM6632A-patchen till upstream Linux.**

Punkt 1–3 kan testas på vår SOtM, som fortsätter fungera som i dag under hela arbetet. Punkt 4 för Pi och x86 behöver en testenhet av varje sort.

## 7. Beslut som behövs

- **Licens:** kärnpatchen och DKMS-modulen måste vara GPL-2.0. Resten kan vara GPL-3.0, som håller derivat öppna, eller MIT och Apache-2.0, som är friare. Typsnitten Jost och Cormorant är OFL och får följa med.
- **Offentligt repo:** när punkt 1 är klar.
- **Namn:** mscOS, och om "mescon" ska synas utåt.
- **Testenheter:** vilka som finns att tillgå utöver SOtM:en.
