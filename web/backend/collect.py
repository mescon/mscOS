"""Read-only collectors for device, audio chain and Soloist state. Runs as root."""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import json
import os
import re
import shlex

SVC_USER = "soloist"
SOLOIST_HOME = "/var/lib/soloist"
SOLOIST_UNIT = f"{SOLOIST_HOME}/.config/systemd/user/soloist.service"
NET_IF = "end0"
BUILD_VALID_DAYS = 90


async def run(cmd: str | list, timeout: float = 15, as_user: bool = False, input_: bytes | None = None):
    """Run a command, return (rc, stdout, stderr) as text."""
    if isinstance(cmd, str):
        cmd = ["bash", "-c", cmd]
    if as_user:
        uid = await soloist_uid()
        # setpriv instead of runuser: no PAM session (no journal spam, less work).
        cmd = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups", "--",
               "env", f"HOME={SOLOIST_HOME}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
               f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"] + cmd
    p = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE if input_ else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(input_), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "", "timeout"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


_uid: int | None = None


async def soloist_uid() -> int:
    global _uid
    if _uid is None:
        import pwd
        _uid = pwd.getpwnam(SVC_USER).pw_uid
    return _uid


def read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


# --------------------------------------------------------------------------- system
def system_info() -> dict:
    mem = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) * 1024 if v.strip() else 0
    pol = "/sys/devices/system/cpu/cpufreq/policy0"
    up = float(read("/proc/uptime", "0 0").split()[0])
    root_fs = ""
    for line in read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) > 2 and parts[1] == "/":
            root_fs = parts[2]
    os_rel = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', read("/etc/os-release"), re.M))
    return {
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "os": os_rel.get("PRETTY_NAME", ""),
        "uptime_s": int(up),
        "load": [float(x) for x in read("/proc/loadavg", "0 0 0").split()[:3]],
        "pmic_temp_c": pmic_temp(),
        "cpu_mhz": int(read(f"{pol}/scaling_cur_freq", "0").strip() or 0) // 1000,
        "governor": read(f"{pol}/scaling_governor").strip(),
        "mem_total": mem.get("MemTotal", 0),
        "mem_available": mem.get("MemAvailable", 0),
        "root_overlay": root_fs == "overlay",
        "net": net_info(),
    }


_temp_cache: tuple[float, float | None] = (0.0, None)


def pmic_temp() -> float | None:
    """AXP209 PMIC die temperature (iio, I2C, ~20 ms), cached 60 s.

    Deliberately NOT /sys/class/thermal/thermal_zone0/temp: the A20 GPADC sensor read
    blocks ~1 s in the kernel on this board and causes audible dropouts.
    """
    global _temp_cache
    import time
    now = time.monotonic()
    if now - _temp_cache[0] < 60:
        return _temp_cache[1]
    val = None
    for d in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
        if "gpadc" in read(f"{d}/name"):
            continue  # the blocking A20 sensor
        raw = read(f"{d}/in_temp_raw").strip()
        if raw.lstrip("-").isdigit():
            off = float(read(f"{d}/in_temp_offset", "0").strip() or 0)
            scale = float(read(f"{d}/in_temp_scale", "1").strip() or 1)
            val = round((int(raw) + off) * scale / 1000, 1)
            break
    _temp_cache = (now, val)
    return val


def net_info() -> dict:
    base = f"/sys/class/net/{NET_IF}"
    ip = ""
    try:
        import socket
        import fcntl
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", NET_IF.encode()))[20:24])
    except OSError:
        pass
    return {
        "iface": NET_IF, "ip": ip, "mac": read(f"{base}/address").strip(),
        "speed_mbps": int(read(f"{base}/speed", "0").strip() or 0),
        "duplex": read(f"{base}/duplex").strip(),
        "state": read(f"{base}/operstate").strip(),
    }


# --------------------------------------------------------------------------- DAC / ALSA
def dac_info() -> dict:
    """Find the USB audio card and describe link speed, formats and current stream."""
    for card in sorted(glob.glob("/proc/asound/card[0-9]*")):
        usbid = read(f"{card}/usbid").strip()
        if not usbid:
            continue
        stream = read(f"{card}/stream0")
        head = stream.splitlines()[0] if stream else ""
        m = re.search(r"^(.*) at (usb-\S+), (\w+) speed", head)
        altsets = []
        for blk in re.findall(r"Altset \d+\n(.*?)(?=\n\s*Interface|\n\s*Capture:|\Z)", stream, re.S):
            fmt = re.search(r"Format: (\S+)", blk)
            rates = re.search(r"Rates: ([^\n]+)", blk)
            bits = re.search(r"Bits: (\d+)", blk)
            if fmt:
                altsets.append({
                    "format": fmt.group(1),
                    "bits": int(bits.group(1)) if bits else None,
                    "rates": [int(r) for r in re.findall(r"\d+", rates.group(1))] if rates else [],
                })
        hw = read(f"{card}/pcm0p/sub0/hw_params").strip()
        hwp = {} if hw in ("", "closed") else dict(
            (k.strip(), v.strip()) for k, v in (l.split(":", 1) for l in hw.splitlines() if ":" in l))
        status = read(f"{card}/pcm0p/sub0/status").strip()
        state = re.search(r"state: (\w+)", status)
        max_rate = max((r for a in altsets for r in a["rates"]), default=0)
        return {
            "present": True,
            "card": os.path.basename(card),
            "name": (m.group(1) if m else read(f"{card}/id").strip()),
            "usb_id": usbid,
            "usb_path": m.group(2) if m else "",
            "usb_speed": (m.group(3) if m else "unknown"),  # high / full
            "max_rate": max_rate,
            "altsets": altsets,
            "hw_params": hwp,
            "state": state.group(1) if state else ("CLOSED" if hw == "closed" else ""),
        }
    return {"present": False}


async def usb_errors_since_boot() -> int:
    rc, out, _ = await run("journalctl -k -b --no-pager -o cat | grep -cE 'error -71|device not accepting address|device descriptor read'")
    try:
        return int(out.strip() or 0)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- PipeWire
async def pipewire_info() -> dict:
    rc, out, _ = await run(["pw-dump"], as_user=True, timeout=10)
    info: dict = {"running": rc == 0, "sink": None, "streams": []}
    if rc != 0:
        return info
    try:
        objs = json.loads(out)
    except json.JSONDecodeError:
        return info
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = o.get("info", {}).get("props", {})
        params = o.get("info", {}).get("params", {})
        fmt = (params.get("Format") or [{}])[0] if params.get("Format") else {}
        props_p = (params.get("Props") or [{}])[0] if params.get("Props") else {}
        node = {
            "id": o.get("id"), "name": props.get("node.name"),
            "description": props.get("node.description") or props.get("media.name"),
            "state": o.get("info", {}).get("state"),
            "format": fmt.get("format"), "rate": fmt.get("rate"), "channels": fmt.get("channels"),
            "volume": (props_p.get("channelVolumes") or [None])[0],
            "mute": props_p.get("mute"),
        }
        cls = props.get("media.class", "")
        if cls == "Audio/Sink" and str(props.get("node.name", "")).startswith("alsa_output.usb"):
            info["sink"] = node
        elif cls == "Stream/Output/Audio":
            node["app"] = props.get("application.name")
            info["streams"].append(node)
    return info


async def pw_top_errors() -> dict:
    """Snapshot of pw-top: per node quantum, rate and cumulative ERR (xrun) counters."""
    rc, out, _ = await run(["pw-top", "-b", "-n", "2"], as_user=True, timeout=8)
    res = {}
    if rc != 0:
        return res
    block = out.strip().split("\nS   ID")[-1]  # last iteration only
    for line in block.splitlines():
        cols = line.split()
        if len(cols) < 9 or cols[0] not in ("R", "I", "S", "C"):
            continue
        name = cols[-1]
        try:
            res[name] = {"state": cols[0], "quantum": int(cols[2]), "rate": int(cols[3]),
                         "err": int(cols[8])}
        except ValueError:
            continue
    return res


# --------------------------------------------------------------------------- Soloist
def soloist_unit_text() -> str:
    return read(SOLOIST_UNIT)


def soloist_device_name() -> str:
    m = re.search(r'--device-name\s+"([^"]+)"', soloist_unit_text())
    return m.group(1) if m else ""


def prefs_path() -> str | None:
    p = sorted(glob.glob(f"{SOLOIST_HOME}/.local/share/soloist/settings/Users/*/prefs"))
    return p[0] if p else None


def normalization_enabled() -> bool | None:
    p = prefs_path()
    if not p:
        return None
    m = re.search(r"^audio\.normalize_v2=(\w+)", read(p), re.M)
    return True if not m else m.group(1) != "false"


_version_cache: tuple[float, str] = (0.0, "")


async def soloist_version() -> str:
    """`soloist --version`, re-run only when the binary changes (it is a 29 MB executable)."""
    global _version_cache
    try:
        mtime = os.stat("/usr/local/bin/soloist").st_mtime
    except OSError:
        return ""
    if mtime != _version_cache[0]:
        rc, out, _ = await run(["/usr/local/bin/soloist", "--version"], timeout=10)
        _version_cache = (mtime, out.strip())
    return _version_cache[1]


async def soloist_info() -> dict:
    version = await soloist_version()
    build = re.search(r"\((\d{8})\)", version)
    days_left = None
    if build:
        bdate = dt.datetime.strptime(build.group(1), "%Y%m%d").date()
        days_left = (bdate + dt.timedelta(days=BUILD_VALID_DAYS) - dt.date.today()).days
    rc2, active, _ = await run(["systemctl", "--user", "is-active", "soloist.service"], as_user=True)
    rc3, rt_active, _ = await run(["systemctl", "--user", "is-active", "soloist-rt.service"], as_user=True)
    rc4, timer, _ = await run("systemctl show soloist-update.timer -p NextElapseUSecRealtime -p LastTriggerUSec --value")
    tl = timer.strip().splitlines()
    return {
        "version": version,
        "build_date": build.group(1) if build else None,
        "days_left": days_left,
        "service": active.strip(),
        "rt_watcher": rt_active.strip(),
        "device_name": soloist_device_name(),
        "normalization": normalization_enabled(),
        "update_next": tl[0] if tl else "",
        "update_last": tl[1] if len(tl) > 1 else "",
        "api_key": api_key_info(),
    }


async def apt_upgradable() -> list[str]:
    rc, out, _ = await run("apt list --upgradable 2>/dev/null | tail -n +2", timeout=60)
    return [l.split("/")[0] for l in out.splitlines() if "/" in l]


async def logs(source: str, lines: int = 200, errors_only: bool = False) -> str:
    lines = max(10, min(int(lines), 2000))
    uid = await soloist_uid()
    q = {
        "soloist": f"journalctl _UID={uid} --user-unit soloist.service",
        "audio": f"journalctl _UID={uid} --user-unit pipewire.service --user-unit wireplumber.service",
        "kernel": "journalctl -k -b",
        "web": "journalctl -u sotm-web.service",
        "system": "journalctl -b",
    }.get(source)
    if not q:
        return ""
    prio = " -p warning" if errors_only else ""
    rc, out, _ = await run(f"{q}{prio} -n {lines} --no-pager -o short-iso", timeout=20)
    # Never expose the Spotify developer key.
    return re.sub(r"(--api-key\s+)\S+", r"\1<dold>", out)


# --------------------------------------------------------------------------- optimisation ledger
SOLOIST_ENV = "/etc/soloist.env"
WP_CONF = f"{SOLOIST_HOME}/.config/wireplumber/wireplumber.conf.d/50-usb-dac.conf"
PW_CONF = f"{SOLOIST_HOME}/.config/pipewire/pipewire.conf.d/10-bitperfect.conf"
QUIET_TIMERS = ("apt-daily.timer", "apt-daily-upgrade.timer", "man-db.timer", "e2scrub_all.timer", "dpkg-db-backup.timer")


def api_key_info() -> dict:
    """Whether a Soloist key is configured (never the key itself)."""
    text = read(SOLOIST_ENV)
    try:
        changed = int(os.stat(SOLOIST_ENV).st_mtime)
    except OSError:
        changed = None
    return {"set": bool(re.search(r"^SOLOIST_API_KEY=\S{8,}", text, re.M)), "changed": changed}


def _threads() -> list[dict]:
    """Audio-relevant threads: owner process, name, policy, RT priority, allowed CPUs."""
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        pcomm = read(f"/proc/{pid}/comm").strip()
        if pcomm not in ("pipewire", "soloist", "wireplumber"):
            continue
        cpus = re.search(r"^Cpus_allowed_list:\s*(\S+)", read(f"/proc/{pid}/status"), re.M)
        for tid in os.listdir(f"/proc/{pid}/task") if os.path.isdir(f"/proc/{pid}/task") else []:
            st = read(f"/proc/{pid}/task/{tid}/stat")
            if ")" not in st:
                continue
            f = st.rsplit(")", 1)[1].split()
            out.append({"proc": pcomm, "name": read(f"/proc/{pid}/task/{tid}/comm").strip(),
                        "rtprio": int(f[37]), "policy": int(f[38]),
                        "cpus": cpus.group(1) if cpus else ""})
    return out


def _irq_affinity() -> dict[str, str]:
    res = {}
    for line in read("/proc/interrupts").splitlines():
        m = re.match(r"\s*(\d+):.*\s([eo]hci_hcd:usb\d+|end0|sunxi-mmc)\s*$", line)
        if m:
            res[m.group(2)] = read(f"/proc/irq/{m.group(1)}/smp_affinity").strip()
    return res


def _kernel_config(opt: str) -> str | None:
    m = re.search(rf"^CONFIG_{opt}=(\S+)", read(f"/boot/config-{os.uname().release}"), re.M)
    return m.group(1) if m else None


def optimizations(status: dict, playback: dict) -> list[dict]:
    """Every tuning measure, verified live on the device. state: ok | warn | optional."""
    cmd = read("/proc/cmdline")
    sysi, pw, solo = status.get("system") or {}, status.get("pipewire") or {}, status.get("soloist") or {}
    sink, top = pw.get("sink") or {}, status.get("pw_top") or {}
    th = _threads()
    irq = _irq_affinity()
    items = []

    def add(group, key, title, why, ok, value, optional=False):
        items.append({"group": group, "id": key, "title": title, "why": why, "value": value,
                      "state": "ok" if ok else ("optional" if optional else "warn")})

    # ---- Kärna och processor
    preempt = "PREEMPT" in os.uname().version and "PREEMPT_NONE" not in os.uname().version
    hz = _kernel_config("HZ")
    add("Kärna och processor", "kernel", "Låglatenskärna",
        "Kärnan kan avbrytas mitt i arbete, så ljudtrådar kommer till direkt.",
        preempt and hz == "1000", f"{'PREEMPT' if preempt else 'Ingen preemption'}, {hz or '?'} Hz")
    rtgs = "rt_group_sched=0" in cmd or _kernel_config("RT_GROUP_SCHED") is None
    add("Kärna och processor", "rtgroup", "Realtidsschemaläggning tillåten",
        "Utan detta får PipeWire ingen realtidsprioritet inne i systemd.", rtgs,
        "Ingen RT-budget per grupp" if rtgs else "RT-budget 0 i cgroups")
    gov, mhz = sysi.get("governor"), sysi.get("cpu_mhz")
    add("Kärna och processor", "cpu", "Fast klockfrekvens",
        "Ingen frekvensväxling som kan ge jitter i bearbetningen.", gov == "performance" and mhz == 960,
        f"{mhz} MHz, {gov}")
    usb_irq = [v for k, v in irq.items() if "hci_hcd" in k]
    audio_cpu = {t["cpus"] for t in th}
    split = bool(usb_irq) and all(v.endswith("2") for v in usb_irq) and audio_cpu == {"1"}
    add("Kärna och processor", "cpu1", "Egen processorkärna för ljud",
        "USB-avbrott och ljudtrådar på kärna 1, nätverk, SD-kort och panel på kärna 0.", split,
        "Kärna 1: USB + PipeWire + Soloist" if split else f"Ljud på kärna {', '.join(sorted(audio_cpu)) or '?'}")
    dl = [t for t in th if t["proc"] == "pipewire" and t["name"].startswith("data-loop") and t["policy"] == 1]
    so = [t for t in th if t["proc"] == "soloist" and t["policy"] == 1]
    add("Kärna och processor", "rt", "Realtidsprioritet",
        "PipeWires ljudtråd och Soloists renderingstrådar går före allt annat.", bool(dl) and len(so) >= 1,
        f"PipeWire {max((t['rtprio'] for t in dl), default=0)}, Soloist {max((t['rtprio'] for t in so), default=0)} ({len(so)} {'tråd' if len(so) == 1 else 'trådar'})")
    add("Kärna och processor", "audit", "Säkerhetsloggning av",
        "Ingen granskningslogg på varje systemanrop.", "audit=0" in cmd or _kernel_config("AUDIT") is None,
        "audit=0" if "audit=0" in cmd else "Aktiv")

    # ---- Ljudväg
    pwc = read(PW_CONF)
    locked = re.search(r"default\.clock\.allowed-rates\s*=\s*\[\s*44100\s*\]", pwc) is not None
    live = sink.get("rate")
    rate_ok = (live in (None, 44100)) and locked and all(s.get("rate") in (None, 44100) for s in pw.get("streams", []))
    add("Ljudväg", "rate", "Ingen omsampling",
        "PipeWire är låst till 44,1 kHz, samma som Spotify Lossless.", rate_ok,
        f"Utgång {sink.get('format')} 44,1 kHz" if live else ("Låst till 44,1 kHz" if locked else "Inte låst"))
    vol = playback.get("volume")
    vol_ok = (vol in (None, 100)) and (sink.get("volume") is None or abs(sink["volume"] - 1) < 1e-6)
    add("Ljudväg", "volume", "Full digital volym",
        "All volym under 100 % räknar om samplen. Volymen sköts i förstärkaren.", vol_ok,
        f"Spotify {vol if vol is not None else '–'} %, PipeWire {round((sink.get('volume') or 0) * 100)} %")
    norm = solo.get("normalization")
    add("Ljudväg", "norm", "Volymnormalisering av",
        "Spotify sänker annars vissa låtar med flera dB.", norm is False,
        "Av" if norm is False else ("På" if norm else "Okänt"))
    wp = read(WP_CONF)
    tsched = "disable-tsched" in wp and re.search(r"api\.alsa\.disable-tsched\s*=\s*true", wp)
    add("Ljudväg", "tsched", "Avbrottsstyrd USB-matning",
        "DAC:ens egna avbrott styr flödet i stället för en uppskattande timer.", bool(tsched),
        "period 1024, headroom 1024" if tsched else "Timerstyrd")
    q = top.get("quantum")
    mq = re.search(r"default\.clock\.quantum\s*=\s*(\d+)", pwc)
    q = q or (int(mq.group(1)) if mq else None)
    add("Ljudväg", "quantum", "Rymlig buffert",
        "1024 sampel ger marginal mot avbrott utan att påverka ljudet.", q == 1024,
        f"{q} sampel ({round(q / 44.1)} ms)" if q else "–")
    add("Ljudväg", "pro", "Rå ljudprofil",
        "Pro-audio-profilen: ingen kanalmixning, ingen dither, inbyggt ljud avstängt.",
        "pro-audio" in wp and "dither.method" in wp, "pro-audio, S32LE")

    # ---- Störningsfrihet
    zones = [z for z in glob.glob("/sys/class/thermal/thermal_zone*") if read(f"{z}/type").strip() == "cpu-thermal"]
    th_ok = not zones or all(read(f"{z}/mode").strip() == "disabled" for z in zones)
    add("Störningsfrihet", "thermal", "Ingen termoavläsning",
        "A20:s sensor blockerar processorn i upp till en sekund per avläsning.", th_ok,
        "Sensorn borttagen" if not zones else ("Avstängd" if th_ok else "Pollas"))
    dt_ok = read("/proc/device-tree/soc/hdmi@1c16000/status").strip("\0\n") == "disabled"
    nmods = max(0, len(read("/proc/modules").splitlines()))
    add("Störningsfrihet", "dt", "Oanvänd hårdvara avstängd",
        "HDMI, GPU, video, SATA, Wi-Fi och Bluetooth får inga klockor och drivrutiner.", dt_ok,
        f"{nmods} kärnmoduler laddade")
    cache = any(l.split()[1] == f"{SOLOIST_HOME}/.cache" and l.split()[2] == "tmpfs"
                for l in read("/proc/mounts").splitlines() if len(l.split()) > 2)
    add("Störningsfrihet", "cache", "Strömningscache i RAM",
        "Inga skrivningar till SD-kortet under uppspelning.", cache, "tmpfs 400 MB" if cache else "På SD-kortet")
    wants = "/etc/systemd/system/timers.target.wants"
    active_t = [t for t in QUIET_TIMERS if os.path.exists(f"{wants}/{t}")]
    add("Störningsfrihet", "timers", "Inga bakgrundsjobb under lyssning",
        "Automatiska apt-, man-db- och diskkontroller är avstängda; underhåll sker 04:30–04:45.",
        not active_t, "Avstängda" if not active_t else f"{len(active_t)} timers aktiva")
    sp = (sysi.get("net") or {}).get("speed_mbps")
    add("Störningsfrihet", "net", "Nätverk i 100 Mbit/s",
        "Lägre länkhastighet ger mindre elektriskt brus; räcker gott för Lossless.", sp == 100,
        f"{sp} Mbit/s" if sp else "–", optional=True)
    add("Störningsfrihet", "overlay", "Skrivskyddat system",
        "SD-kortet tål strömavbrott och systemet är identiskt efter varje start.",
        bool(sysi.get("root_overlay")), "På" if sysi.get("root_overlay") else "Av", optional=True)
    return items


# --------------------------------------------------------------------------- DAC detail (the /dacinfo/ view)
# Which socket on the sMS-200Ultra's rear panel each A20 USB controller feeds.
PORTS = {"1c1c000.usb": "Audio USB", "1c1c400.usb": "Audio USB",
         "1c14000.usb": "USB (via intern hubb)", "1c14400.usb": "USB (via intern hubb)"}
UAC = {0x20: "USB Audio Class 2.0", 0x00: "USB Audio Class 1.0", 0x30: "USB Audio Class 3.0"}


def _usb_card() -> tuple[str, str] | None:
    """(/proc/asound/cardN, /sys/bus/usb/devices/<dev>) for the first USB audio card."""
    for card in sorted(glob.glob("/proc/asound/card[0-9]*")):
        if read(f"{card}/usbid").strip():
            n = card.rsplit("card", 1)[1]
            intf = os.path.realpath(f"/sys/class/sound/card{n}/device")
            return card, os.path.dirname(intf)
    return None


def dac_clock(card: str | None = None) -> dict | None:
    """The DAC's own sample clock, measured through the asynchronous feedback endpoint.

    In async mode the DAC tells the host how many samples per USB packet it consumes. The
    kernel shows it as 16.16 fixed point ("0x2c.1998"); times packets per second it is the
    DAC crystal's real rate relative to the host's USB frame clock.
    """
    if card is None:
        c = _usb_card()
        if not c:
            return None
        card = c[0]
    s = read(f"{card}/stream0")
    m = re.search(r"Momentary freq = (\d+) Hz \(0x([0-9a-f]+)\.([0-9a-f]+)\)", s)
    if not m:
        return None
    per_packet = int(m.group(2), 16) + int(m.group(3), 16) / 65536
    iv = re.search(r"Data packet interval: (\d+) us", s)
    pps = 1e6 / int(iv.group(1)) if iv else 1000.0  # full speed: one packet per 1 ms frame
    hz = per_packet * pps
    hw = read(f"{card}/pcm0p/sub0/hw_params")
    nom = re.search(r"rate: (\d+)", hw)
    nominal = int(nom.group(1)) if nom else round(hz / 100) * 100
    fb = re.search(r"Feedback Format = (\d+)\.(\d+)", s)
    frac_bits = int(fb.group(2)) if fb else 16
    res_hz = pps / (1 << frac_bits)
    return {"hz": round(hz, 4), "nominal": nominal, "ppm": round((hz - nominal) / nominal * 1e6, 2),
            "res_ppm": round(res_hz / nominal * 1e6, 2), "feedback_format": fb.group(0).split("= ")[1] if fb else None}


async def dac_detail() -> dict:
    c = _usb_card()
    if not c:
        return {"present": False}
    card, dev = c
    sysf = lambda f: read(f"{dev}/{f}").strip()
    ctrl = next((p for p in os.path.realpath(dev).split("/") if p.endswith(".usb")), "")
    proto = None
    for i in sorted(glob.glob(f"{dev}/*:1.*")):
        if read(f"{i}/bInterfaceClass").strip() == "01":
            proto = int(read(f"{i}/bInterfaceProtocol").strip() or "0", 16)
            break
    stream = read(f"{card}/stream0")
    status = read(f"{card}/pcm0p/sub0/status")
    cur_alt = re.search(r"Altset = (\d+)", status + stream)
    # the running altset's endpoint description
    ep = {}
    if cur_alt:
        blk = re.search(rf"Altset {cur_alt.group(1)}\n(.*?)(?=\n  Interface|\Z)", stream, re.S)
        if blk:
            for key, rx in (("endpoint", r"Endpoint: (.+)"), ("sync_endpoint", r"Sync Endpoint: (\S+)"),
                            ("implicit", r"Implicit Feedback Mode: (\w+)")):
                m = re.search(rx, blk.group(1))
                if m:
                    ep[key] = m.group(1).strip()
    pk = re.search(r"Packet Size = (\d+)", stream)
    n = card.rsplit("card", 1)[1]
    rc, mix, _ = await run(["amixer", "-c", n, "contents"], timeout=5)
    controls = {}
    for name, val in re.findall(r"name='([^']+)'.*?: values=(\S+)", mix, re.S):
        controls[name] = val
    clock_valid = next((v for k, v in controls.items() if k.startswith("Clock Source") and "Validity" in k), None)
    raw = {}
    for title, cmd_ in (("aplay -l", "aplay -l"), ("lsusb", "lsusb"), ("lsusb -t", "lsusb -t"),
                        ("/proc/asound/cards", f"cat /proc/asound/cards"), (f"{card}/stream0", f"cat {card}/stream0"),
                        ):
        _, out, _ = await run(cmd_, timeout=5)
        raw[title] = out
    raw["amixer"] = mix
    return {
        "present": True,
        "manufacturer": sysf("manufacturer"), "product": sysf("product"),
        "usb_id": f"{sysf('idVendor')}:{sysf('idProduct')}",
        "firmware": (lambda b: f"{int(b[:2], 16)}.{b[2:]}" if len(b) == 4 else b)(sysf("bcdDevice")),
        "usb_version": sysf("version"), "max_power": sysf("bMaxPower"),
        "speed_mbps": int(float(sysf("speed") or 0)),
        "controller": ctrl, "port": PORTS.get(ctrl, ctrl),
        "host": "EHCI (USB 2.0)" if "1c1c000" in ctrl or "1c14000" in ctrl else "OHCI (USB 1.1)" if ctrl else "",
        "via_hub": "1c14" in ctrl,
        "uac": UAC.get(proto, f"protokoll {proto:#x}" if proto is not None else "–"),
        "altset": int(cur_alt.group(1)) if cur_alt else None,
        "packet_size": int(pk.group(1)) if pk else None,
        **ep,
        "clock": dac_clock(card),
        "clock_valid": clock_valid,
        "output_jack": controls.get("Speaker - Output Jack"),
        "events": await usb_events(),
        "raw": raw,
    }


async def usb_events() -> list[dict]:
    """USB connect/disconnect and failed high-speed negotiations this boot, newest last."""
    rc, out, _ = await run("journalctl -k -b --no-pager -o short-unix | grep -E "
                           "\"usb [0-9]+-[0-9.]+: (new .*USB device|USB disconnect|device descriptor read|device not accepting|New USB device found|Product:)\"",
                           timeout=10)
    ev: list[dict] = []
    for line in out.splitlines():
        m = re.match(r"(\d+)\.\d+ \S+ kernel: usb (\d+)-([0-9.]+): (.*)", line)
        if not m:
            continue
        t, bus, port, msg = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        if msg.startswith("new "):
            sp = re.search(r"new (\S+) USB", msg)
            ev.append({"t": t, "bus": bus, "kind": "attempt", "speed": sp.group(1) if sp else "", "errors": 0})
        elif "error" in msg and ev and ev[-1]["kind"] == "attempt":
            ev[-1]["errors"] += 1
        elif msg.startswith("Product:") and ev and ev[-1]["kind"] == "attempt":
            ev[-1].update(kind="connected", product=msg.split(": ", 1)[1])
        elif msg.startswith("USB disconnect"):
            ev.append({"t": t, "bus": bus, "kind": "disconnect"})
    # fold repeated failed attempts into one event per negotiation
    out_ev: list[dict] = []
    for e in ev:
        if e["kind"] == "attempt":
            if out_ev and out_ev[-1]["kind"] == "failed" and out_ev[-1]["speed"] == e["speed"] and e["t"] - out_ev[-1]["t"] < 30:
                out_ev[-1]["tries"] += 1; out_ev[-1]["errors"] += e["errors"]
            else:
                out_ev.append({**e, "kind": "failed", "tries": 1})
        else:
            out_ev.append(e)
    return [e for e in out_ev if not (e["kind"] == "connected" and "root hub" in e.get("product", "").lower())][-30:]


# --------------------------------------------------------------------------- supply (AXP209 ACIN)
def supply() -> dict | None:
    """Input voltage/current of the sMS-200Ultra's DC jack, from the AXP209 PMIC ADC."""
    for d in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
        v, i = read(f"{d}/in_voltage0_raw").strip(), read(f"{d}/in_current0_raw").strip()
        if v.isdigit() and i.isdigit():
            vs = float(read(f"{d}/in_voltage0_scale", "1").strip() or 1)
            cs = float(read(f"{d}/in_current0_scale", "1").strip() or 1)
            V, A = int(v) * vs / 1000, int(i) * cs / 1000
            return {"v": round(V, 3), "a": round(A, 3), "w": round(V * A, 2)}
    return None


# --------------------------------------------------------------------------- LEDs, link speed
def leds_state() -> bool:
    return any(read(f"{d}/trigger").find("[none]") < 0 or read(f"{d}/brightness").strip() not in ("", "0")
               for d in glob.glob("/sys/class/leds/*:usr"))


def apply_leds(on: bool):
    """Off: every user LED dark. On: Armbian's default, green blinks with SD-card activity."""
    for d in glob.glob("/sys/class/leds/*:usr"):
        green = "green" in os.path.basename(d)
        try:
            with open(f"{d}/trigger", "w") as f:
                f.write("mmc0" if on and green else "none")
            if not (on and green):
                with open(f"{d}/brightness", "w") as f:
                    f.write("0")
        except OSError:
            pass


async def apply_net_speed(mbps: int):
    adv = "0x008" if mbps == 100 else "0x02f"  # 100/Full only, or everything up to 1000/Full
    await run(["ethtool", "-s", NET_IF, "advertise", adv], timeout=10)


# --------------------------------------------------------------------------- DAC clock, long-term
def pcm_counter() -> dict | None:
    """ALSA's frame counter for the USB DAC with its monotonic timestamp.

    In async mode the host sends exactly what the DAC consumes, so over hours hw_ptr advances
    at the DAC crystal's rate. The reference is the A20's 24 MHz crystal (CLOCK_MONOTONIC).
    Each reading is only accurate to a few ms (hw_ptr moves per URB), so the rate is only
    meaningful over long spans: ±0.4 ppm after 3 h.
    """
    c = _usb_card()
    if not c:
        return None
    st = read(f"{c[0]}/pcm0p/sub0/status")
    trig, ts, hw = (re.search(rx, st) for rx in (r"trigger_time: ([\d.]+)", r"tstamp\s*: ([\d.]+)", r"hw_ptr\s*: (\d+)"))
    rate = re.search(r"rate: (\d+)", read(f"{c[0]}/pcm0p/sub0/hw_params"))
    if not (trig and ts and hw and rate):
        return None
    return {"trig": float(trig.group(1)), "ts": float(ts.group(1)), "hw": int(hw.group(1)), "nominal": int(rate.group(1))}


CLOCK_EDGE_S = 0.004  # timing uncertainty per reading


def clock_stats(hist: list, now: dict | None) -> dict | None:
    """hist rows: [wall_t, ts, hw, nominal, trig]. Average since the stream started, and a
    rolling one-hour rate for the chart."""
    if now is None:
        return None
    rows = [r for r in hist if len(r) == 5]
    seg = []
    for r in reversed(rows):  # the trailing run belonging to the current stream
        if r[4] != now["trig"] or r[3] != now["nominal"] or (seg and r[2] > seg[-1][2]):
            break
        seg.append(r)
    seg.reverse()
    out = {"nominal": now["nominal"], "series": []}
    if seg and now["ts"] - seg[0][1] > 60:
        el = now["ts"] - seg[0][1]
        hz = (now["hw"] - seg[0][2]) / el
        out.update(hz=round(hz, 4), ppm=round((hz - now["nominal"]) / now["nominal"] * 1e6, 2),
                   unc_ppm=round(CLOCK_EDGE_S * 2 / el * 1e6, 2), elapsed_s=int(el))
    # rolling one-hour rates, every segment
    j = 0
    for i, r in enumerate(rows):
        while j < i and (rows[j][4] != r[4] or r[1] - rows[j][1] > 3600):
            j += 1
        a = rows[j]
        if a[4] == r[4] and r[1] - a[1] >= 1800 and r[2] >= a[2]:
            hz = (r[2] - a[2]) / (r[1] - a[1])
            out["series"].append([r[0], round((hz - r[3]) / r[3] * 1e6, 2)])
    return out


# --------------------------------------------------------------------------- USB re-negotiation
async def usb_reconnect() -> dict:
    """Unbind and rebind the DAC's EHCI/OHCI pair, so the kernel negotiates the link again
    (useful after changing cable or port). The USB IRQs go back to CPU1 afterwards."""
    c = _usb_card()
    ctrl = next((p for p in os.path.realpath(c[1]).split("/") if p.endswith(".usb")), "") if c else ""
    base = int(ctrl.split(".")[0], 16) & ~0x400 if ctrl else 0x1c1c000
    ehci, ohci = f"{base:x}.usb", f"{base + 0x400:x}.usb"
    d = "/sys/bus/platform/drivers"
    script = (f"echo {ohci} > {d}/ohci-platform/unbind; echo {ehci} > {d}/ehci-platform/unbind; sleep 1; "
              f"echo {ehci} > {d}/ehci-platform/bind; echo {ohci} > {d}/ohci-platform/bind; sleep 9; "
              "awk -F: '/[eo]hci_hcd:usb/ {gsub(/ /, \"\", $1); print $1}' /proc/interrupts | "
              "while read -r i; do echo 2 > /proc/irq/$i/smp_affinity; done")
    rc, _, err = await run(script, timeout=40)
    info = dac_info()
    return {"ok": rc == 0, "error": err.strip(), "present": info.get("present", False), "speed": info.get("usb_speed")}


# --------------------------------------------------------------------------- source switch: Spotify | Roon
# One player owns the DAC at a time. Spotify = the soloist user's PipeWire stack + Soloist;
# Roon = Roon Bridge talking to ALSA directly (needed for DSD). The socket is stopped too, or
# the panel's own pw-dump/pw-top calls would re-activate PipeWire and grab the DAC again.
SPOTIFY_UNITS = ["pipewire.socket", "pipewire.service", "wireplumber.service", "soloist.service", "soloist-rt.service"]
ROON_UNIT = "roonbridge.service"


def pcm_closed() -> bool:
    c = _usb_card()
    return not c or read(f"{c[0]}/pcm0p/sub0/status").strip() in ("", "closed")


async def _wait_released(secs: float = 6) -> bool:
    for _ in range(int(secs / 0.2)):
        if pcm_closed():
            return True
        await asyncio.sleep(0.2)
    return pcm_closed()


async def roon_installed() -> bool:
    return os.path.exists("/opt/RoonBridge/start.sh") and os.path.exists(f"/etc/systemd/system/{ROON_UNIT}")


async def roon_active() -> bool:
    rc, out, _ = await run(["systemctl", "is-active", ROON_UNIT], timeout=5)
    return out.strip() == "active"


async def set_source(src: str) -> dict:
    """Hand the DAC to Spotify or to Roon. Returns what is running afterwards."""
    if src == "roon":
        if not await roon_installed():
            raise RuntimeError("Roon Bridge är inte installerat")
        rc, _, err = await run(["systemctl", "--user", "stop", *reversed(SPOTIFY_UNITS)], as_user=True, timeout=60)
        released = await _wait_released()
        rc2, _, err2 = await run(["systemctl", "start", ROON_UNIT], timeout=60)
        ok_ = rc2 == 0
        detail = (err + err2).strip()
    else:
        rc, _, err = await run(["systemctl", "stop", ROON_UNIT], timeout=60)
        released = await _wait_released()
        rc2, _, err2 = await run(["systemctl", "--user", "start", *SPOTIFY_UNITS], as_user=True, timeout=60)
        ok_ = rc2 == 0
        detail = (err + err2).strip()
    return {"ok": ok_, "source": src, "dac_released": released, "roon_active": await roon_active(), "error": detail[:300]}
