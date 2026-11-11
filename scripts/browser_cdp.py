"""Private inherited-pipe CDP IO; never an externally exposed protocol endpoint."""
import contextlib
import json
import os
import queue
import re
import threading


class Fault(Exception):
    def __init__(self, kind, message=''):
        self.kind = kind
        super().__init__(message or kind)


class CDP:
    def __init__(self, read, write):
        self.read, self.write = read, write
        self.counter = 0
        self.lock = threading.Lock()
        self.pending = {}
        self.closed = False
        self.authorize = contextlib.nullcontext
        self.fds_closed = False
        self.thread = threading.Thread(target=self.receive, daemon=True)
        self.thread.start()

    @staticmethod
    def deliver(waiter, message):
        answer, compact = waiter
        if compact:
            # Input receipts need only ACK/error, never retain arbitrary CDP
            # result payloads. At most64 such compact completions per controller.
            message = ({'error': {'message': str(message['error'])[:1024]}}
                       if 'error' in message else {'result': {}})
        try:
            answer.put_nowait(message)
        except queue.Full:
            pass  # EOF cannot replace a response already delivered to this waiter.

    def receive(self):
        buf = bytearray()
        dropping = False
        try:
            while not self.closed:
                chunk = os.read(self.read, 65536)
                if not chunk:
                    break
                parts = chunk.split(b'\0')
                for index, part in enumerate(parts):
                    if not dropping and len(buf) + len(part) > 16 * 1024 * 1024:
                        # Drain oversized whole messages without poisoning the
                        # next response or allocating an unbounded PNG string.
                        match = re.search(rb'"id"\s*:\s*(\d+)', buf[:512])
                        with self.lock:
                            waiter = self.pending.get(int(match[1])) if match else None
                        if waiter:
                            self.deliver(waiter, {'error': {'message': 'CDP response exceeds 16MiB; reduce viewport'}})
                        buf.clear()
                        dropping = True
                    if not dropping:
                        buf.extend(part)
                    if index == len(parts) - 1:
                        continue
                    if dropping:
                        dropping = False
                        continue
                    message = json.loads(buf)
                    buf.clear()
                    with self.lock:
                        waiter = self.pending.get(message.get('id'))
                    if waiter:
                        self.deliver(waiter, message)
                    # The frontend has its own native host session. No pull-v2
                    # event consumer exists, so never retain diagnostic events.
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True
            with self.lock:
                for waiter in self.pending.values():
                    self.deliver(waiter, {'error': {'message': 'Chromium pipe closed; inspect chromium.stderr'}})

    def close(self):
        with self.lock:
            if self.fds_closed:
                return
            self.fds_closed = True
            self.closed = True
        os.close(self.write)
        self.thread.join(timeout=1)
        os.close(self.read)

    def begin(self, method, params=None, session=None, compact=False):
        answer = queue.Queue(maxsize=1)
        with self.authorize(), self.lock:
            if self.closed:
                raise Fault('browser_failed', 'Chromium pipe closed')
            self.counter += 1
            ident = self.counter
            self.pending[ident] = (answer, compact)
            message = {'id': ident, 'method': method, 'params': params or {}}
            if session:
                message['sessionId'] = session
            data = json.dumps(message, separators=(',', ':')).encode() + b'\0'
            try:
                while data:
                    data = data[os.write(self.write, data):]
            except Exception:
                self.pending.pop(ident, None)
                raise
        return ident, answer

    def forget(self, ident):
        with self.lock:
            self.pending.pop(ident, None)

    def call(self, method, params=None, session=None, timeout=5):
        ident, answer = self.begin(method, params, session)
        try:
            response = answer.get(timeout=timeout)
            if 'error' in response:
                raise Fault('browser_failed', str(response['error']))
            return response.get('result', {})
        except queue.Empty:
            raise Fault('browser_failed', 'Chromium command timed out: ' + method)
        finally:
            self.forget(ident)
