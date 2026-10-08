"""
runner.py — the only bridge between the interface thread and background work.

Workers never touch Tk. They call post(kind, payload); the window drains the queue on its own timer. One job runs at
a time (resolving a link, downloading, tidying genres …), each with its own stop Event, so closing the window or
pressing Stop is always just `stop.set()` followed by a bounded wait.
"""
import logging
import queue
import threading
import time

log = logging.getLogger("musicdl")


class Runner:
    def __init__(self):
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self.name = ""

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def post(self, kind, payload=None):
        self.q.put((kind, payload))

    def start(self, name, fn):
        """Run fn(stop, post) on a worker thread. Posts ('error', (name, message)) if it raises, always ('done', name)."""
        if self.busy():
            return False
        self.stop = threading.Event()
        self.name = name
        stop = self.stop

        def body():
            try:
                fn(stop, self.post)
            except Exception as e:                                   # a worker must never die silently
                log.exception("%s failed", name)
                self.post("error", (name, str(e) or e.__class__.__name__))
            finally:
                self.post("done", name)
        self.thread = threading.Thread(target=body, name=f"worker-{name}", daemon=True)
        self.thread.start()
        return True

    def cancel(self):
        self.stop.set()

    def wait(self, seconds):
        """Wait (bounded) for the worker to wind down; True if it has."""
        t = self.thread
        if t is not None:
            t.join(seconds)
        return not self.busy()

    def drain(self, limit=300):
        """Up to `limit` queued messages, oldest first. Event floods can't starve the window: it takes a slice per tick."""
        out = []
        try:
            while len(out) < limit:
                out.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return out

    def backlog(self):
        return self.q.qsize()


def wait_until(predicate, seconds, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
