"""
autotune.py — look at this computer and this connection once in a while, then pick how many songs to process at once.

Runs in the background shortly after start-up when "Automatic" is on (at most once a week). Scans the hardware (cores,
RAM: see resources.py), measures latency to archive.org and download speed over a few parallel streams from
Cloudflare's public speed-test endpoint, and turns that into the number of songs a run starts with and a retry count.
During a long run resources.Governor then moves that number up or down by what it sees. If the scan or the plan ever
fails, the older rule below (half the cores, ~10 Mbps a song, at most 8) is used instead.
"""
import os
import socket
import threading
import time

import requests

from . import platform_, resources

SPEED_URLS = ("https://speed.cloudflare.com/__down?bytes=25000000", "https://speed.cloudflare.com/__down?bytes=10000000")
MAX_AUTO_PARALLEL = 8            # remote services throttle heavy users; the manual setting goes higher
MBPS_PER_STREAM = 10             # headroom each concurrent download is assumed to need
STALE_AFTER = 7 * 86400


def detect_hardware():
    try:
        return resources.detect().info()
    except Exception:
        return {"cores": os.cpu_count() or 2, "ram_gb": platform_.total_ram_gb()}


def measure_latency(host="archive.org", port=443, tries=3):
    """Best TCP connect time in ms, or None when offline."""
    best = None
    for _ in range(tries):
        t = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=4):
                ms = (time.perf_counter() - t) * 1000
                best = ms if best is None else min(best, ms)
        except OSError:
            continue
    return round(best) if best is not None else None


def measure_throughput(seconds=3.0, streams=3):
    """Aggregate download speed in Mbps over a few seconds (several streams so fast links are filled)."""
    total, lock = [0], threading.Lock()
    deadline = time.perf_counter() + seconds

    def pull(url):
        try:
            with requests.get(url, stream=True, timeout=(5, 5)) as r:
                for chunk in r.iter_content(65536):
                    with lock:
                        total[0] += len(chunk)
                    if time.perf_counter() >= deadline:
                        return
        except requests.RequestException:
            return

    start = time.perf_counter()
    threads = [threading.Thread(target=pull, args=(SPEED_URLS[i % len(SPEED_URLS)],), daemon=True) for i in range(streams)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 6)
    elapsed = max(0.2, time.perf_counter() - start)
    return None if total[0] < 100_000 else round(total[0] * 8 / elapsed / 1e6, 1)


def recommend(hw, net):
    """Measurements -> settings. Conservative when something could not be measured."""
    mbps, lat = net.get("mbps"), net.get("latency_ms")
    try:
        parallel = resources.plan(resources.Hardware.from_info(hw), mbps).start
    except Exception:                                        # the older rule, kept as the fallback
        cores, ram = int(hw.get("cores") or 2), hw.get("ram_gb")
        by_cpu = max(1, cores // 2)
        if ram is not None and ram < 4:
            by_cpu = min(by_cpu, 2)
        by_net = 2 if mbps is None else max(1, int(mbps // MBPS_PER_STREAM))
        parallel = max(1, min(by_cpu, by_net, MAX_AUTO_PARALLEL))
    flaky = lat is None or lat > 200 or (mbps is not None and mbps < 5)
    return {"parallel": parallel, "retries": 3 if flaky else 2}


def optimize():
    """Measure everything: {'parallel', 'retries', 'info': {...}}."""
    hw = detect_hardware()
    net = {"latency_ms": measure_latency(), "mbps": measure_throughput()}
    rec = recommend(hw, net)
    return {"parallel": rec["parallel"], "retries": rec["retries"], "info": dict(hw, **net, measured_at=int(time.time()))}


def due(settings):
    """Worth measuring now? (Automatic is on and the last measurement is missing or a week old.)"""
    if not settings.auto:
        return False
    return time.time() - settings.auto_info.get("measured_at", 0) > STALE_AFTER or not settings.auto_parallel


def describe(info):
    """Short human line for Settings: '8 cores · 16 GB · 94 Mbps · 21 ms' ('12 cores (4+8)' when there are kinds)."""
    if not info:
        return ""
    tiers = info.get("tiers") or []
    bits = [f"{info.get('cores', '?')} cores" + (f" ({'+'.join(str(n) for _, n in tiers)})" if len(tiers) > 1 else "")]
    if info.get("ram_gb"):
        bits.append(f"{info['ram_gb']:g} GB")
    if info.get("mbps"):
        bits.append(f"{info['mbps']:.0f} Mbps")
    if info.get("latency_ms"):
        bits.append(f"{info['latency_ms']} ms")
    return " · ".join(bits)
