"""FIFO provider admission; expired waiters cannot strand the queue."""
from collections import deque
import threading
import time


class OrderedSlots:
    def __init__(self, capacity):
        self.capacity = max(1, capacity)
        self.active = 0
        self.waiting = deque()
        self.condition = threading.Condition()

    def acquire(self, timeout=None, *, check=None):
        expires = None if timeout is None else time.monotonic() + max(0, timeout)
        token = object()
        with self.condition:
            self.waiting.append(token)
            try:
                while True:
                    if check is not None:
                        check()
                    if self.waiting[0] is token and self.active < self.capacity:
                        self.waiting.popleft()
                        self.active += 1
                        self.condition.notify_all()
                        return True
                    remaining = None if expires is None else expires - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self.condition.wait(min(.05,remaining) if remaining is not None and check is not None
                                        else .05 if check is not None else remaining)
            finally:
                if token in self.waiting:
                    self.waiting.remove(token)
                    self.condition.notify_all()

    def release(self):
        with self.condition:
            if self.active <= 0:
                raise ValueError('Provider slot released without acquisition')
            self.active -= 1
            self.condition.notify_all()
