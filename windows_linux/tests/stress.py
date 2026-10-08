"""
Stress run (not part of the unit tests):  py -3 tests/stress.py [songs] [parallel]

Pushes a few hundred songs through the real engine (fake sources served from localhost, real ffmpeg encoding, real tag
and cover writing) and checks the things that go wrong in long runs: memory that keeps growing, threads that never
exit, leftover temp files, results that go missing, and a Stop in the middle of a busy run.
"""
import os
import sys
import threading
import time

from helpers import FileServer, fresh_dir, make_audio, noise_cover

from musicdl import platform_
from musicdl.config import Settings
from musicdl.core import catalog, engine, netio, sources
from musicdl.core.models import Track
from musicdl.ui.app import RunState

N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
PAR = int(sys.argv[2]) if len(sys.argv) > 2 else 8
WWW = fresh_dir("stress_www")
OUT = fresh_dir("stress_out")


def main():
    make_audio(os.path.join(WWW, "a.mp3"), 20, "-b:a", "128k")
    noise_cover(os.path.join(WWW, "cover.png"), 300)
    srv = FileServer(WWW)
    srv.delay = 0.05
    netio.DEEZER_LIMIT.interval = netio.WEB_LIMIT.interval = netio.CAA_LIMIT.interval = 0.0
    cand = {"source": "archive.org", "id": "t:a", "url": srv.url + "a.mp3", "ext": ".mp3", "seconds": 20, "kbps": 128,
            "lossless": False, "score": 1, "title": "a"}
    sources.archive_candidates = lambda rc, stop, fits, deep: [cand]
    sources.youtube_candidates = lambda *a, **k: []
    catalog.lookup = lambda lib, track, stop, want_genre=False: {
        "durations": [], "covers": [srv.url + "cover.png"], "album": "Stress", "year": "2024", "track_no": 1, "disc_no": 1,
        "isrc": "", "genre": "", "t": time.time()}

    st = Settings()
    st.auto, st.parallel, st.min_kbps, st.verify, st.mode, st.fmt, st.bitrate = False, PAR, 0, False, "advanced", "aac", 128
    tracks = [Track(f"Song {i:04d}", f"Artist {i % 40}", duration=20.0) for i in range(N)]

    base_threads = threading.active_count()
    rss0 = platform_.process_rss_mb()
    samples, run = [], RunState(N, OUT)
    stop = threading.Event()
    seen = {"result": 0, "begin": 0}
    lock = threading.Lock()

    def emit(ev):
        with lock:
            seen[ev["type"]] = seen.get(ev["type"], 0) + 1
        if ev["type"] == "result":
            res = ev["result"]
            run.done += 1
            run.counts[res.status] = run.counts.get(res.status, 0) + 1
            run.rows.append({"title": res.track, "thumb": res.thumb})

    t0 = time.time()
    worker = threading.Thread(target=lambda: engine.Job(tracks, OUT, st, emit=emit, stop=stop).run(), name="stress-job")
    worker.start()
    while worker.is_alive():
        time.sleep(5)
        samples.append((round(time.time() - t0), run.done, round(platform_.process_rss_mb(), 1), threading.active_count()))
        print("t=%3ds  done=%4d  rss=%6.1f MB  threads=%d" % samples[-1], flush=True)
    worker.join()
    took = time.time() - t0
    files = [f for _r, _d, fs in os.walk(OUT) for f in fs if f.endswith(".m4a")]
    time.sleep(1.0)
    rss1 = platform_.process_rss_mb()
    print(f"\n{N} songs, {PAR} at once: {took:.0f}s ({N / took:.1f} songs/s), counts={run.counts}, output files={len(files)}")
    print(f"RSS {rss0:.0f} -> {rss1:.0f} MB; threads {base_threads} -> {threading.active_count()}; "
          f"tmp left={os.path.exists(os.path.join(OUT, '.musicdl_tmp'))}")
    mid = samples[len(samples) // 3][2] if len(samples) > 3 else rss0
    problems = []
    if run.counts.get("ok", 0) != N or len(files) != N:
        problems.append("not every song was saved")
    if seen["result"] != N:
        problems.append(f"{seen['result']} result events for {N} songs")
    if rss1 > mid + 60:
        problems.append(f"memory kept growing ({mid:.0f} -> {rss1:.0f} MB)")
    if threading.active_count() > base_threads + 2:
        problems.append("threads left running")
    if os.path.exists(os.path.join(OUT, ".musicdl_tmp")):
        problems.append("temp folder left behind")

    # second run over the same folder: everything must be recognised, nothing downloaded again
    began = []
    t1 = time.time()
    engine.Job(tracks, OUT, st, emit=lambda e: began.append(e) if e["type"] == "begin" else None).run()
    print(f"re-run over the finished folder: {time.time() - t1:.1f}s, {len(began)} songs started again")
    if began:
        problems.append("re-run redid finished songs")

    # Stop in the middle of a busy run: prompt, clean, no stray threads
    shutil_out = fresh_dir("stress_out2")
    stop2 = threading.Event()
    done2 = []
    w2 = threading.Thread(target=lambda: engine.Job(tracks, shutil_out, st, emit=lambda e: done2.append(1) if e["type"] == "result" else None,
                                                  stop=stop2).run())
    w2.start()
    time.sleep(4)
    stop2.set()
    t2 = time.time()
    w2.join(30)
    print(f"stop mid-run: wound down in {time.time() - t2:.1f}s after {len(done2)} songs, alive={w2.is_alive()}")
    if w2.is_alive() or time.time() - t2 > 15:
        problems.append("Stop was slow or hung")
    if os.path.exists(os.path.join(shutil_out, ".musicdl_tmp")):
        problems.append("Stop left a temp folder")
    partial = [f for _r, _d, fs in os.walk(shutil_out) for f in fs if f.endswith((".part", ".tmp"))]
    if partial:
        problems.append(f"Stop left partial files: {partial[:3]}")
    srv.close()
    print("\nPROBLEMS:" if problems else "\nOK — no problems found", *problems, sep="\n  ")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

