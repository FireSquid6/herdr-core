import importlib.util
from pathlib import Path
import unittest
import threading
import os
import json
import queue
import time

spec = importlib.util.spec_from_file_location('worker', Path(__file__).with_name('browser_worker.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class Policy(unittest.TestCase):
    def test_oversized_cdp_message_is_drained_without_poisoning_next_call(self):
        read, peer_write = os.pipe()
        peer_read, write = os.pipe()
        cdp = w.CDP(read, write)
        def send(data):
            while data:
                data = data[os.write(peer_write, data):]
        def peer():
            pending = bytearray()
            def request():
                while b'\0' not in pending:
                    pending.extend(os.read(peer_read, 65536))
                raw, _, rest = pending.partition(b'\0')
                pending[:] = rest
                return json.loads(raw)
            first = request()
            send(('{"id":' + str(first['id']) + ',"result":{"data":"').encode())
            block = b'A' * 65536
            for _ in range(257):
                send(block)
            send(b'"}}\0')
            second = request()
            send(json.dumps({'id':second['id'],'result':{'alive':True}}).encode()+b'\0')
            os.close(peer_write)
            os.close(peer_read)
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(w.Fault, 'exceeds 16MiB'):
                cdp.call('Page.captureScreenshot')
            self.assertEqual(cdp.call('Browser.getVersion'), {'alive':True})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            cdp.close()

    def test_workspace_is_not_path(self):
        for bad in ['', '../a', '/', 'a.b', 'é', 'a' * 129]:
            with self.assertRaises(w.Fault):
                w.workspace(bad)
        self.assertEqual(w.workspace('ws_A-2'), 'ws_A-2')

    def test_owner_expiry_and_generation(self):
        lease = w.Lease()
        first = lease.acquire('a', False, False, 1)
        self.assertEqual(first, lease.acquire('a', False, False, 2))
        with self.assertRaises(w.Fault):
            lease.acquire('b', False, False, 2)
        second = lease.acquire('b', True, True, 3)
        self.assertNotEqual(first, second)
        with self.assertRaises(w.Fault):
            lease.check('a', first, 3)
        with self.assertRaises(w.Fault):
            lease.check('b', second, 34)
        self.assertNotEqual(second, lease.acquire('b', False, False, 34))

    def test_expiry_serializes_against_renewal_and_new_grant(self):
        for takeover in (False, True):
            b = w.Browser.__new__(w.Browser)
            b.lease = w.Lease()
            b.state_lock = threading.RLock()
            b.local = threading.local()
            closed = []
            b.close_frontends = lambda: closed.append(True)
            first = b.lease.acquire('a', False, False, 1)
            barrier = threading.Barrier(2)
            expired = []
            def expire():
                barrier.wait()
                expired.append(b.expire_due(32))
            with b.state_lock:
                thread = threading.Thread(target=expire)
                thread.start()
                barrier.wait()  # expiry has started but cannot inspect the lease
                if takeover:
                    expected = b.lease.acquire('b', True, True, 32)
                else:
                    b.lease.until = 62  # monotonic heartbeat under the same lock
                    expected = first
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(expired, [False])
            self.assertEqual(closed, [])
            self.assertEqual(b.lease.generation, expected)
            self.assertTrue(b.expire_due(63))
            self.assertEqual(closed, [True])
            self.assertIsNone(b.lease.owner)

    def test_force_requires_confirmation_even_without_owner(self):
        lease = w.Lease()
        with self.assertRaises(w.Fault):
            lease.acquire('a', True, False, 1)
        self.assertIsNone(lease.owner)

    def test_viewport_physical_bound(self):
        self.assertEqual(w.viewport({'width': 800, 'height': 600, 'scale': 1}), (800, 600, 1))
        for p in [{'width': 4096, 'height': 4096, 'scale': 3},
                  {'width': 0, 'height': 600, 'scale': 1},
                  {'width': 800, 'height': 600, 'scale': float('nan')}]:
            with self.assertRaises(w.Fault):
                w.viewport(p)

    def test_urls_exclude_internal_privilege(self):
        for url in ['file:///etc/passwd', 'javascript:alert(1)', 'devtools://devtools/', 'chrome://settings', 'https://a\n']:
            with self.assertRaises(w.Fault):
                w.navigation(url)
        self.assertEqual(w.navigation('http://localhost:8000/'), 'http://localhost:8000/')


class InputReceipts(unittest.TestCase):
    def browser(self):
        class Transport:
            def __init__(self):
                self.pending = {}
                self.next = 0
                self.reply = None
            def begin(self, *args, **kwargs):
                self.next += 1
                answer = queue.Queue(1)
                self.pending[self.next] = answer
                if self.reply is not None:
                    answer.put(self.reply)
                return self.next, answer
            def forget(self, ident):
                self.pending.pop(ident, None)
        b = w.Browser.__new__(w.Browser)
        b.state_lock = threading.RLock()
        b.lease = w.Lease()
        generation = b.lease.acquire('a', False, False)
        b.local = threading.local()
        b.local.identity, b.local.generation = 'a', generation
        b.input_receipts, b.input_errors = {}, []
        b.cdp = Transport()
        return b, generation

    def test_late_error_visible_once_only_to_original_current_controller(self):
        b, generation = self.browser()
        result = b.input_command('Input.dispatchMouseEvent', {}, 'session')
        self.assertTrue(result['pending'])
        b.cdp.pending[result['receipt']].put({'error': {'message': 'late rejection'}})
        with self.assertRaises(w.Fault):
            b.heartbeat_receipts('other', generation)
        status = b.heartbeat_receipts('a', generation)
        self.assertEqual(status['input_errors'][0]['kind'], 'cdp_error')
        self.assertIn('late rejection', status['input_errors'][0]['message'])
        self.assertEqual(status['input_pending'], 0)
        self.assertEqual(b.heartbeat_receipts('a', generation)['input_errors'], [])

    def test_immediate_ack_error_is_not_reported_as_accepted(self):
        b, _ = self.browser()
        b.cdp.reply = {'error': {'message': 'invalid input'}}
        with self.assertRaisesRegex(w.Fault, 'invalid input'):
            b.input_command('Input.dispatchKeyEvent', {}, 'session')
        self.assertEqual(b.input_receipts, {})

    def test_revocation_discards_pending_receipts_before_replacement_owner(self):
        b, old = self.browser()
        result = b.input_command('Input.dispatchMouseEvent', {}, 'session')
        answer = b.cdp.pending[result['receipt']]
        b.discard_inputs()
        new = b.lease.acquire('b', True, True)
        self.assertNotEqual(new, old)
        answer.put({'error': {'message': 'old-owner secret error'}})
        self.assertEqual(b.heartbeat_receipts('b', new), {'input_pending': 0, 'input_errors': []})

    def test_receipt_capacity_and_timeout_do_not_silently_drop_errors(self):
        b, generation = self.browser()
        now = time.monotonic()
        for ident in range(64):
            answer = queue.Queue(1)
            b.cdp.pending[ident] = answer
            b.input_receipts[ident] = {'owner': 'a', 'generation': generation,
                'deadline': now - 1, 'answer': answer}
        b.reap_inputs(now)
        self.assertEqual(len(b.input_errors), 64)
        with self.assertRaises(w.Fault):
            b.input_command('Input.dispatchKeyEvent', {}, 'session')
        errors = b.heartbeat_receipts('a', generation)['input_errors']
        self.assertEqual(len(errors), 64)
        self.assertTrue(all(error['kind'] == 'timeout' for error in errors))
        self.assertEqual(b.input_errors, [])


if __name__ == '__main__':
    unittest.main()
