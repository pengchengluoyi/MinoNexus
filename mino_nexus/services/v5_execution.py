"""Shared monotonic budget and immutable first stop reason."""
from __future__ import annotations

import threading
import time


class ExecutionControl:
    def __init__(self, wall_sec):
        self.started = time.monotonic()
        self.deadline = self.started + wall_sec
        self.lock = threading.RLock()
        self.state = "running"
        self.reason_code = ""
        self.action_ref = {}

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def stop(self, reason, action_ref=None):
        with self.lock:
            if self.state == "running":
                self.state, self.reason_code, self.action_ref = "write_stopped", reason, action_ref or {}
            return {"stop_run": True, "reason_code": self.reason_code, "action_ref": self.action_ref}

    def terminate(self):
        with self.lock:
            self.state = "terminal"
