"""Read-only collectors for device, audio chain and Soloist state. Runs as root."""
from __future__ import annotations

import asyncio
import logging
import datetime as dt
import glob
import ipaddress
import json
import os
import re
import shlex
import socket

import board
from i18n import N_, T, decimal



def _audio_user() -> tuple[str, str]:
    """The user that owns the audio session (PipeWire and the players): MSCOS_AUDIO_USER,
    else "mscos"; "soloist" on installs from before sources were modules."""
    import pwd
    for name in (os.environ.get("MSCOS_AUDIO_USER"), "mscos", "soloist"):
        try:
            if name:
                return name, pwd.getpwnam(name).pw_dir
        except KeyError:
            pass
    return "mscos", "/var/lib/mscos-audio"


SVC_USER, AUDIO_HOME = _audio_user()
LIB = os.environ.get("MSCOS_LIB", "/usr/lib/mscos")  # install scripts of the sources live here


def net_if() -> str:
    """The wired interface: the one with the default route, else the first ethernet."""
    for line in read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 1 and f[1] == "00000000":
            return f[0]
    for n in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
        if n.startswith(("en", "eth")):
            return n
    return ""




async def run(cmd: str | list, timeout: float = 15, as_user: bool = False, input_: bytes | None = None):
    """Run a command, return (rc, stdout, stderr) as text."""
    if isinstance(cmd, str):
        cmd = ["bash", "-c", cmd]
    if as_user:
        uid = await audio_uid()
        # setpriv instead of runuser: no PAM session (no journal spam, less work).
        cmd = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups", "--",
               "env", f"HOME={AUDIO_HOME}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
               f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"] + cmd
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE if input_ else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:  # the program is not installed (a minimal system): a failed command, not a crash
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(p.communicate(input_), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "", "timeout"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


_uid: int | None = None


async def audio_uid() -> int:
    global _uid
    if _uid is None:
        import pwd
        _uid = pwd.getpwnam(SVC_USER).pw_uid
    return _uid


STATE_DIR = os.environ.get("MSCOS_STATE") or os.environ.get("SOTM_STATE") or "/var/lib/mscos"


async def read_capped(resp, cap: int) -> bytes:
    """The whole body of an HTTP answer, and at most one byte more than `cap` (which says
    "too large"). Not `resp.content.read(n)`: that returns what has arrived so far, which
    for anything but a small answer is only its beginning."""
    out = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        out += chunk
        if len(out) > cap:
            break
    return bytes(out[:cap + 1])


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
        "mscos": dict(re.findall(r'^(\w+)="?([^"\n]*)"?', read("/etc/mscos-release"), re.M)).get("VERSION", ""),
        "uptime_s": int(up),
        "load": [float(x) for x in read("/proc/loadavg", "0 0 0").split()[:3]],
        "pmic_temp_c": pmic_temp(),
        "cpus": os.cpu_count() or 1,
        "board": os.path.basename(os.path.realpath("/etc/mscos/board.conf")).removesuffix(".conf") if os.path.exists("/etc/mscos/board.conf") else "",
        "cpu_mhz": int(read(f"{pol}/scaling_cur_freq", "0").strip() or 0) // 1000,
        "governor": read(f"{pol}/scaling_governor").strip(),
        "mem_total": mem.get("MemTotal", 0),
        "mem_available": mem.get("MemAvailable", 0),
        "root_overlay": root_fs == "overlay" and os.path.isdir("/media/root-ro"),
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
    if val is None and not board.profile()["THERMAL_OFF"]:
        raw = read("/sys/class/thermal/thermal_zone0/temp").strip()
        if raw.lstrip("-").isdigit():
            val = round(int(raw) / 1000, 1)
    _temp_cache = (now, val)
    return val


def net_info() -> dict:
    NET_IF = net_if()
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
            fmt = re.search(r"Format: (?:SPECIAL )?(\S+)", blk)
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


# --------------------------------------------------------------------------- packages, logs
async def apt_upgradable() -> list[str]:
    rc, out, _ = await run("apt list --upgradable 2>/dev/null | tail -n +2", timeout=60)
    return [l.split("/")[0] for l in out.splitlines() if "/" in l]


async def logs(source: str, lines: int = 200, errors_only: bool = False, player=((), ())) -> str:
    """player: the current source's (user units, system units)."""
    lines = max(10, min(int(lines), 2000))
    uid = await audio_uid()
    if not all(re.fullmatch(r"[\w@.\-]+", u) for u in (*player[0], *player[1])):
        return ""
    def user_units(units) -> str:
        # by journal field, as root: what the unit's processes wrote, and what the user's systemd
        # said about it. (--user-unit matches the caller's own user, so it finds nothing here.)
        return "journalctl " + " + ".join(f"_UID={uid} _SYSTEMD_USER_UNIT={u} + _UID={uid} USER_UNIT={u}" for u in units)

    pl = user_units(player[0]) if player[0] else \
        ("journalctl " + " ".join(f"-u {u}" for u in player[1])) if player[1] else None
    q = {
        "player": pl, "soloist": pl,
        "audio": user_units(("pipewire.service", "wireplumber.service")),
        "kernel": "journalctl -k -b",
        "web": "journalctl -u mscos-panel.service",
        "system": "journalctl -b",
    }.get(source)
    if not q:
        return ""
    prio = " -p warning" if errors_only else ""
    rc, out, _ = await run(f"{q}{prio} -n {lines} --no-pager -o short-iso", timeout=20)
    # Never expose the Spotify developer key.
    return re.sub(r"(--api-key\s+)\S+", r"\1<dold>", out)


# --------------------------------------------------------------------------- optimisation ledger
WP_CONF = f"{AUDIO_HOME}/.config/wireplumber/wireplumber.conf.d/50-usb-dac.conf"
PW_CONF = f"{AUDIO_HOME}/.config/pipewire/pipewire.conf.d/10-bitperfect.conf"
QUIET_TIMERS = ("apt-daily.timer", "apt-daily-upgrade.timer", "man-db.timer", "e2scrub_all.timer", "dpkg-db-backup.timer")


# Processes whose threads matter for audio; a source module adds its player (SourcePlugin.rt_proc).
RT_PROCS = {"pipewire", "wireplumber"}


def _threads() -> list[dict]:
    """Audio-relevant threads: owner process, name, policy, RT priority, allowed CPUs."""
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        pcomm = read(f"/proc/{pid}/comm").strip()
        if pcomm not in RT_PROCS:
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
        m = re.match(r"\s*(\d+):", line)
        if m and re.search(board.profile()["USB_IRQ_MATCH"], line):
            res[f"usb:{m.group(1)}"] = read(f"/proc/irq/{m.group(1)}/smp_affinity").strip()
    return res


def _kernel_config(opt: str) -> str | None:
    m = re.search(rf"^CONFIG_{opt}=(\S+)", read(f"/boot/config-{os.uname().release}"), re.M)
    return m.group(1) if m else None


def optimizations(status: dict, playback: dict, vol_bridge: bool = False, output: str = "",
                  source: dict | None = None) -> list[dict]:
    """Every tuning measure, verified live on the device. state: ok | warn | optional.
    Which measures apply follows the board profile (the CPU split, a blocking thermal sensor,
    a device tree overlay) and the current source (through PipeWire, or the DAC directly)."""
    cmd = read("/proc/cmdline")
    bp = board.profile()
    sysi, pw = status.get("system") or {}, status.get("pipewire") or {}
    sink, top = pw.get("sink") or {}, status.get("pw_top") or {}
    src = source or {}
    via_pw = bool(src.get("pipewire", True)) and bool(src)
    rt_proc, label = src.get("rt_proc") or "", src.get("label") or ""  # the player's own realtime threads, if it has any
    th = _threads()
    irq = _irq_affinity()
    items = []

    def add(group, key, title, why, ok, value, optional=False):
        items.append({"group": group, "id": key, "title": title, "why": why, "value": value,
                      "state": "ok" if ok else ("optional" if optional else "warn")})

    # ---- Kärna och processor
    G = T("Kärna och processor")
    preempt = "PREEMPT" in os.uname().version and "PREEMPT_NONE" not in os.uname().version
    hz = _kernel_config("HZ")
    add(G, "kernel", T("Låglatenskärna"),
        T("Kärnan kan avbrytas mitt i arbete, så ljudtrådar kommer till direkt."),
        preempt and hz in ("1000", None), f"{'PREEMPT' if preempt else T('Ingen preemption')}, {hz or '?'} Hz")
    rtgs = "rt_group_sched=0" in cmd or _kernel_config("RT_GROUP_SCHED") is None
    add(G, "rtgroup", T("Realtidsschemaläggning tillåten"),
        T("Utan detta får PipeWire ingen realtidsprioritet inne i systemd."), rtgs,
        T("Ingen RT-budget per grupp") if rtgs else T("RT-budget 0 i cgroups"))
    gov, mhz = sysi.get("governor"), sysi.get("cpu_mhz")
    fixed = bp["CPU_FIXED_KHZ"]
    if gov:  # no cpufreq at all (some PCs and VMs): nothing to check
        add(G, "cpu", T("Fast klockfrekvens"),
            T("Ingen frekvensväxling som kan ge jitter i bearbetningen."),
            gov == "performance" and (not fixed or mhz == int(fixed) // 1000), f"{mhz} MHz, {gov}")
    acpu = board.audio_cpu()
    if acpu is not None:
        audio_cpu = {t["cpus"] for t in th}
        mask = 1 << acpu
        # no USB controller interrupt by that name (a profile that does not fit, a VM): the threads decide
        usb_ok = all(int(v.replace(",", ""), 16) == mask for v in irq.values())
        split = usb_ok and (audio_cpu == {str(acpu)} or not via_pw)
        who = "USB + PipeWire" + (f" + {label}" if rt_proc else "") if via_pw else "USB"
        add(G, "cpu1", T("Egen processorkärna för ljud"),
            T("USB-avbrott och ljudtrådar på kärna {n}; nätverk, lagring och panel på de andra.", n=acpu), split,
            T("Kärna {n}: {who}", n=acpu, who=who) if split else T("Ljud på kärna {cpus}", cpus=", ".join(sorted(audio_cpu)) or "?"))
    if via_pw:
        dl = [t for t in th if t["proc"] == "pipewire" and t["name"].startswith("data-loop") and t["policy"] == 1]
        so = [t for t in th if t["proc"] == rt_proc and t["policy"] == 1]
        val = f"PipeWire {max((t['rtprio'] for t in dl), default=0)}"
        if rt_proc:
            val += f", {label} {max((t['rtprio'] for t in so), default=0)} ({len(so)} {T('tråd') if len(so) == 1 else T('trådar')})"
        add(G, "rt", T("Realtidsprioritet"),
            T("PipeWires ljudtråd och spelarens renderingstrådar går före allt annat."),
            bool(dl) and (len(so) >= 1 or not rt_proc), val)
    add(G, "audit", T("Säkerhetsloggning av"),
        T("Ingen granskningslogg på varje systemanrop."), "audit=0" in cmd or _kernel_config("AUDIT") is None,
        "audit=0" if "audit=0" in cmd else T("Aktiv"))

    # ---- Ljudväg
    G = T("Ljudväg")
    pwc = read(PW_CONF)
    wp = read(WP_CONF)
    if via_pw:
        locked = re.search(r"default\.clock\.allowed-rates\s*=\s*\[\s*44100\s*\]", pwc) is not None
        live = sink.get("rate")
        rate_ok = (live in (None, 44100)) and locked and all(s.get("rate") in (None, 44100) for s in pw.get("streams", []))
        add(G, "rate", T("Ingen omsampling"),
            T("PipeWire är låst till {khz} kHz, samma som Spotify.", khz=decimal(44.1)), rate_ok,
            T("Utgång {format} {khz} kHz", format=sink.get("format"), khz=decimal(44.1)) if live else (T("Låst till {khz} kHz", khz=decimal(44.1)) if locked else T("Inte låst")))
        sink_ok = sink.get("volume") is None or abs(sink["volume"] - 1) < 1e-6
        slider = src.get("slider")  # the source's own volume slider, if it has one that is on
        if slider:
            vol = slider.get("volume")
            if vol_bridge:  # the Spotify slider drives the amplifier; the stream is kept at unity
                add(G, "volume", T("Full digital volym"),
                    T("Spotify-reglaget styr förstärkaren. Strömmen hålls på 100 %, så samplen räknas aldrig om."),
                    sink_ok, T("Spotify {vol} % styr {amp}, strömmen 100 %", vol=vol if vol is not None else "–", amp=output or T("förstärkaren")))
            else:
                add(G, "volume", T("Full digital volym"),
                    T("All volym under 100 % räknar om samplen. Volymen sköts i förstärkaren."), (vol in (None, 100)) and sink_ok,
                    f"Spotify {vol if vol is not None else '–'} %, PipeWire {round((sink.get('volume') or 0) * 100)} %")
            for r in src.get("rows") or []:  # the source's own measures (volume normalization, ...)
                add(G, r["id"], r["title"], r["why"], r["ok"], r["value"])
        else:
            add(G, "volume", T("Full digital volym"),
                T("All volym under 100 % räknar om samplen. Volymen sköts i förstärkaren."), sink_ok,
                f"PipeWire {round((sink.get('volume') if sink.get('volume') is not None else 1) * 100)} %")
        tsched = "disable-tsched" in wp and re.search(r"api\.alsa\.disable-tsched\s*=\s*true", wp)
        add(G, "tsched", T("Avbrottsstyrd USB-matning"),
            T("DAC:ens egna avbrott styr flödet i stället för en uppskattande timer."), bool(tsched),
            "period 1024, headroom 1024" if tsched else T("Timerstyrd"))
        q = top.get("quantum")
        mq = re.search(r"default\.clock\.quantum\s*=\s*(\d+)", pwc)
        q = q or (int(mq.group(1)) if mq else None)
        add(G, "quantum", T("Rymlig buffert"),
            T("1024 sampel ger marginal mot avbrott utan att påverka ljudet."), q == 1024,
            T("{n} sampel ({ms} ms)", n=q, ms=round(q / 44.1)) if q else "–")
        add(G, "pro", T("Rå ljudprofil"),
            T("Pro-audio-profilen: ingen kanalmixning, ingen dither, inbyggt ljud avstängt."),
            "pro-audio" in wp and "dither.method" in wp, "pro-audio, S32LE")
    elif src:
        # PipeWire is stopped: nothing between the player and the DAC (looked up now, not in
        # the status snapshot, which may be from before the source was switched)
        free = not any(t["proc"] == "pipewire" for t in th)
        add(G, "direct", T("Direkt till DAC:n"),
            T("{name} öppnar DAC:n själv: inget mellanled, alla format DAC:n tar emot.", name=src.get("label", "")),
            free, T("PipeWire är stoppat") if free else T("PipeWire håller DAC:n"))

    # ---- Störningsfrihet
    G = T("Störningsfrihet")
    if bp["THERMAL_OFF"]:
        zones = [z for z in glob.glob("/sys/class/thermal/thermal_zone*") if read(f"{z}/type").strip() == bp["THERMAL_OFF"]]
        th_ok = not zones or all(read(f"{z}/mode").strip() == "disabled" for z in zones)
        add(G, "thermal", T("Ingen termoavläsning"),
            T("Kortets temperatursensor blockerar processorn i upp till en sekund per avläsning."), th_ok,
            T("Sensorn borttagen") if not zones else (T("Avstängd") if th_ok else T("Pollas")))
    if bp["DT_OFF_NODE"]:
        dt_ok = read(f"/proc/device-tree/{bp['DT_OFF_NODE']}/status").strip("\0\n") == "disabled"
        nmods = max(0, len(read("/proc/modules").splitlines()))
        add(G, "dt", T("Oanvänd hårdvara avstängd"),
            T("Hårdvara som enheten inte har får inga klockor och drivrutiner."), dt_ok,
            T("{n} kärnmoduler laddade", n=nmods))
    cache = any(l.split()[1] == f"{AUDIO_HOME}/.cache" and l.split()[2] == "tmpfs"
                for l in read("/proc/mounts").splitlines() if len(l.split()) > 2)
    add(G, "cache", T("Strömningscache i RAM"),
        T("Inga skrivningar till lagringen under uppspelning."), cache, "tmpfs 400 MB" if cache else T("På disk"))
    wants = "/etc/systemd/system/timers.target.wants"
    active_t = [t for t in QUIET_TIMERS if os.path.exists(f"{wants}/{t}")]
    add(G, "timers", T("Inga bakgrundsjobb under lyssning"),
        T("Automatiska apt-, man-db- och diskkontroller är avstängda; underhåll sker 04:30–04:45."),
        not active_t, T("Avstängda") if not active_t else T("{n} timers aktiva", n=len(active_t)))
    sp = (sysi.get("net") or {}).get("speed_mbps")
    if sp and sp > 0:
        add(G, "net", T("Nätverk i 100 Mbit/s"),
            T("Lägre länkhastighet ger mindre elektriskt brus; räcker gott för Lossless."), sp == 100,
            f"{sp} Mbit/s", optional=True)
    add(G, "overlay", T("Skrivskyddat system"),
        T("Lagringen tål strömavbrott och systemet är identiskt efter varje start."),
        bool(sysi.get("root_overlay")), T("På") if sysi.get("root_overlay") else T("Av"), optional=True)
    return items


# --------------------------------------------------------------------------- DAC detail (the /dacinfo/ view)
# Which socket on the sMS-200Ultra's rear panel each A20 USB controller feeds.
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


def _host_controller(dev: str) -> str:
    """The kind of host controller the DAC hangs on, from its root hub."""
    bus = read(f"{dev}/busnum").strip()
    root = f"/sys/bus/usb/devices/usb{bus}"
    speed = read(f"{root}/speed").strip()
    drv = os.path.basename(os.path.realpath(f"{root}/../driver")) if bus else ""
    kind = {"12": "USB 1.1", "480": "USB 2.0"}.get(speed, f"USB 3 ({speed} Mbit/s)" if speed else "")
    name = "xHCI" if "xhci" in drv else "EHCI" if "ehci" in drv else "OHCI" if "ohci" in drv else drv
    return f"{name} ({kind})" if name and kind else name or kind


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
        "controller": ctrl, "port": board.usb_ports().get(ctrl, ctrl or f"USB {sysf('busnum')}-{sysf('devpath')}"),
        "host": _host_controller(dev),
        "via_hub": "." in sysf("devpath"),
        "uac": UAC.get(proto, T("protokoll {proto}", proto=f"{proto:#x}") if proto is not None else "–"),
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
               for d in _leds())


def _leds() -> list[str]:
    g = board.profile()["LEDS"]
    return glob.glob(f"/sys/class/leds/{g}") if g else []


def apply_leds(on: bool):
    """Off: the board's LEDs dark. On: the default, a green or activity LED blinks with the SD card."""
    for d in _leds():
        green = "green" in os.path.basename(d) or os.path.basename(d) == "ACT"
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
    if net_if():
        await run(["ethtool", "-s", net_if(), "advertise", adv], timeout=10)


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
    """Make the kernel negotiate the DAC's USB link again (after changing cable or port).
    On controllers that come as an EHCI/OHCI platform pair (Allwinner) the pair is unbound
    and bound again, so a device that fell back to USB 1.1 gets a new try at USB 2.0;
    elsewhere the device is de-authorized and authorized again. The USB interrupts go back
    to the audio core afterwards (the boot-time tuning is run again)."""
    c = _usb_card()
    if not c:
        return {"ok": False, "error": "", "present": False, "speed": None}
    ctrl = next((p for p in os.path.realpath(c[1]).split("/") if p.endswith(".usb")), "")
    d = "/sys/bus/platform/drivers"
    if re.fullmatch(r"[0-9a-f]+\.usb", ctrl) and os.path.isdir(f"{d}/ehci-platform"):
        base = int(ctrl.split(".")[0], 16) & ~0x400
        ehci, ohci = f"{base:x}.usb", f"{base + 0x400:x}.usb"
        script = (f"echo {ohci} > {d}/ohci-platform/unbind; echo {ehci} > {d}/ehci-platform/unbind; sleep 1; "
                  f"echo {ehci} > {d}/ehci-platform/bind; echo {ohci} > {d}/ohci-platform/bind; sleep 9; ")
    else:
        dev = shlex.quote(os.path.realpath(c[1]))
        script = f"echo 0 > {dev}/authorized; sleep 1; echo 1 > {dev}/authorized; sleep 6; "
    rc, _, err = await run(script + f"[ ! -x {LIB}/bin/mscos-tuning ] || {LIB}/bin/mscos-tuning", timeout=40)
    info = dac_info()
    return {"ok": rc == 0, "error": err.strip(), "present": info.get("present", False), "speed": info.get("usb_speed")}


# --------------------------------------------------------------------------- source switch: Spotify | Roon
# One player owns the DAC at a time. Spotify = the soloist user's PipeWire stack + Soloist;
# Roon = Roon Bridge talking to ALSA directly (needed for DSD). The socket is stopped too, or
# the panel's own pw-dump/pw-top calls would re-activate PipeWire and grab the DAC again.


def pcm_closed() -> bool:
    c = _usb_card()
    return not c or read(f"{c[0]}/pcm0p/sub0/status").strip() in ("", "closed")


async def _wait_released(secs: float = 6) -> bool:
    for _ in range(int(secs / 0.2)):
        if pcm_closed():
            return True
        await asyncio.sleep(0.2)
    return pcm_closed()


PIPEWIRE_UNITS = ["pipewire.socket", "pipewire.service", "wireplumber.service"]


async def pipewire(verb: str) -> str:
    """start | stop the audio user's PipeWire session; returns stderr."""
    units = PIPEWIRE_UNITS if verb == "start" else list(reversed(PIPEWIRE_UNITS))
    _, _, err = await run(["systemctl", "--user", verb, *units], as_user=True, timeout=60)
    return err.strip()


async def units_active(units, user: bool) -> bool:
    if not units:
        return False
    cmd = ["systemctl", *(["--user"] if user else []), "is-active", units[0]]
    _, out, _ = await run(cmd, as_user=user, timeout=8)
    return out.strip() == "active"


async def spawn_as_user(cmd: list[str]):
    """A long-running process as the soloist user (PipeWire session), stdout piped."""
    uid = await audio_uid()
    return await asyncio.create_subprocess_exec(
        "setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups", "--",
        "env", f"HOME={AUDIO_HOME}", f"XDG_RUNTIME_DIR=/run/user/{uid}", *cmd,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)


async def local_network() -> tuple[str | None, set[str]]:
    """The LAN of the default route as a CIDR (at most a /22, so a scan stays small), and
    the addresses not to probe: this device and the gateway."""
    rc, out, _ = await run(["ip", "-j", "route", "show", "default"], timeout=5)
    try:
        r = json.loads(out)[0]
        dev, gw = r["dev"], r.get("gateway")
        rc, out, _ = await run(["ip", "-j", "-4", "addr", "show", "dev", dev], timeout=5)
        a = next(i for i in json.loads(out)[0]["addr_info"] if i.get("family") == "inet")
    except (ValueError, IndexError, KeyError, StopIteration):
        return None, set()
    net = ipaddress.ip_network(f"{a['local']}/{max(int(a['prefixlen']), 22)}", strict=False)
    return str(net), {a["local"]} | ({gw} if gw else set())


def _first_line(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read().replace("\0", "").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def detect_streamer() -> str:
    """What this device is: the image's board name (/etc/mscos/board-name, written by the
    image or the installer: device trees often name the reference board, e.g. a SOtM
    sMS-200 reports "Cubietech Cubietruck"), else the PC's DMI vendor and model, else the
    device tree model, else the host name."""
    n = _first_line("/etc/mscos/board-name")
    if n:
        return n
    vendor, product = _first_line("/sys/class/dmi/id/sys_vendor"), _first_line("/sys/class/dmi/id/product_name")
    junk = {"", "to be filled by o.e.m.", "default string", "system product name", "system manufacturer"}
    if product.lower() not in junk:
        return f"{vendor} {product}".strip() if vendor.lower() not in junk and not product.startswith(vendor) else product
    return _first_line("/proc/device-tree/model") or socket.gethostname()


def detect_dac() -> str:
    """The DAC as ALSA names it ("HARMAN USB2.0 High-Speed True HD Audio"), or ""."""
    try:
        with open("/proc/asound/cards") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    for i, line in enumerate(lines):
        if "USB-Audio" in line and i + 1 < len(lines):
            return re.split(r" at usb-", lines[i + 1].strip())[0]
    return ""
