"""Actual finite scheduler checks; stdlib/native provider admission, no wires/settings."""
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

ROOT = Path(__file__).parent
S = load('_scheduler402_native', ROOT / 'search/acquisition_scheduler.py')
A = load('_admission402_native', ROOT / 'search/admission.py')

class DiscoveryParallelTests(unittest.TestCase):
    def test_two_discovery_overlap_and_three_total_with_card_progress(self):
        gate = threading.Barrier(3, timeout=2)
        lock = threading.Lock()
        active = {'all': 0, 'discovery': 0}
        peak = dict(active)
        entered = []
        def call(name, kind):
            with lock:
                active['all'] += 1
                active['discovery'] += kind == 'discovery'
                peak.update({k: max(peak[k], active[k]) for k in peak})
                entered.append(name)
            try:
                if name in {'d1', 'd2', 'card'}:
                    gate.wait()
                return name
            finally:
                with lock:
                    active['all'] -= 1
                    active['discovery'] -= kind == 'discovery'
        def product(name, kind):
            value = yield S.Operation(kind, (), lambda: call(name, kind))
            return value
        products = {n: product(n, 'card' if n == 'card' else 'discovery') for n in ('d1', 'd2', 'd3', 'card')}
        result = S.run_products(products, control=lambda: None)
        self.assertEqual(peak, {'all': 3, 'discovery': 2})
        self.assertEqual(set(result), set(products))
        self.assertEqual(set(entered[:3]), {'d1', 'd2', 'card'})

    def test_oldest_ready_select_card_keeps_priority(self):
        order = []
        def old():
            index = yield S.SelectCard(((0, 'supplier-a'),))
            self.assertEqual(index, 0)
            yield S.Operation('card', ('supplier-a',), lambda: order.append('old-card'))
            return 'old'
        def young():
            yield S.Operation('proof', ('supplier-b',), lambda: order.append('young-proof'))
            return 'young'
        self.assertEqual(S.run_products({'old': old(), 'young': young()}, control=lambda: None, workers=1), {'old': 'old', 'young': 'young'})
        self.assertEqual(order, ['old-card', 'young-proof'])

    def test_same_host_exclusive_and_each_product_sequence(self):
        lock = threading.Lock()
        active = [0]
        seen = {n: [] for n in ('a', 'b', 'c')}
        def call(name, index):
            with lock:
                active[0] += 1
                self.assertEqual(active[0], 1)
            try:
                time.sleep(.005)
                return index
            finally:
                with lock: active[0] -= 1
        def product(name):
            for index in range(3):
                value = yield S.Operation('own_discovery' if index == 0 else 'card', ('same-supplier',), lambda i=index: call(name, i))
                seen[name].append(value)
            return tuple(seen[name])
        result = S.run_products({n: product(n) for n in seen}, control=lambda: None)
        self.assertEqual(result, {n: (0, 1, 2) for n in seen})

    def test_own_discovery_is_not_accidentally_capped_at_two(self):
        gate = threading.Barrier(3, timeout=2)
        def product():
            yield S.Operation('own_discovery', (), lambda: gate.wait())
            return 'done'
        result = S.run_products({str(i): product() for i in range(3)}, control=lambda: None)
        self.assertEqual(list(result.values()), ['done'] * 3)

    def test_native_provider_capacity_one_remains_authoritative(self):
        slots = A.OrderedSlots(1)
        scheduler_gate = threading.Barrier(2, timeout=2)
        lock = threading.Lock()
        inside = [0]
        peak = [0]
        admitted = []
        def call(name):
            scheduler_gate.wait()  # Proves both scheduler operations were granted.
            self.assertTrue(slots.acquire(timeout=2))
            try:
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                    admitted.append(name)
                time.sleep(.01)
            finally:
                with lock: inside[0] -= 1
                slots.release()
            return name
        def product(name):
            return (yield S.Operation('discovery', (), lambda: call(name)))
        result = S.run_products({n: product(n) for n in ('a', 'b')}, control=lambda: None)
        self.assertEqual(peak[0], 1)
        self.assertEqual(set(admitted), {'a', 'b'})
        self.assertEqual(result, {'a': 'a', 'b': 'b'})

    def test_parked_before_guard_never_calls_transport(self):
        calls = []
        def wire():
            calls.append('wire')
            raise AssertionError('parked operation reached wire')
        def product():
            try:
                yield S.Operation('discovery', (), wire, before=lambda: False)
            except S.OperationParked:
                return 'parked'
            raise AssertionError('parked operation acknowledged')
        self.assertEqual(S.run_products({'a': product()}, control=lambda: None), {'a': 'parked'})
        self.assertEqual(calls, [])

    def test_unknown_operation_is_delivered_once_without_replay(self):
        class UnknownWire(Exception): pass
        calls = []
        def wire():
            calls.append('uncertain')
            raise UnknownWire()
        def product():
            try:
                yield S.Operation('discovery', (), wire)
            except UnknownWire:
                return 'unknown_preserved'
        self.assertEqual(S.run_products({'a': product()}, control=lambda: None), {'a': 'unknown_preserved'})
        self.assertEqual(calls, ['uncertain'])

    def test_control_stop_blocks_unstarted_operations(self):
        class LeaseLost(Exception): pass
        calls = []
        stopped = [False]
        def control():
            if stopped[0]: raise LeaseLost()
        def first():
            def work():
                calls.append('first')
                stopped[0] = True
            try:
                yield S.Operation('discovery', (), work)
            except LeaseLost:
                return 'lease_lost'
            return 'first_done'
        waiting_entered = []
        def waiting():
            waiting_entered.append(True)
            try:
                yield S.Operation('discovery', (), lambda: calls.append('forbidden-second'))
            except LeaseLost:
                return 'lease_lost'
        products = {'a': first(), 'b': waiting()}
        with self.assertRaises(LeaseLost):
            S.run_products(products, control=control, workers=1)
        self.assertNotIn('forbidden-second', calls)
        self.assertEqual(waiting_entered, [])
        import inspect
        self.assertTrue(all(inspect.getgeneratorstate(p) == inspect.GEN_CLOSED for p in products.values()))

if __name__ == '__main__':
    unittest.main()
