import unittest
from eject_sleep_protocol import run


class FakeBackend:
    volumes = ["E", "folder-mounted-volume"]

    def __init__(self, fail=None):
        self.calls = []
        self.handles = set()
        self.fail = fail
        self.offline_state = False

    def action(self, name):
        self.calls.append(name)
        if name == self.fail:
            raise OSError(name)

    def validate(self): self.action("validate")
    def open_disk(self):
        self.action("open_disk")
        self.handles.add("disk")
        return "disk"

    def open_volume(self, name):
        self.action("open:" + name)
        self.handles.add(name)
        return name

    def lock(self, handle): self.action("lock:" + handle)
    def flush_volume(self, handle): self.action("flush:" + handle)
    def dismount(self, handle): self.action("dismount:" + handle)
    def offline(self, disk, value):
        self.action("offline" if value else "online")
        self.offline_state = value
    def verify_offline(self, disk): self.action("verify_offline")
    def flush_disk(self, disk): self.action("flush_disk")
    def sleep(self, disk): self.action("sleep")
    def close(self, handle):
        self.action("close:" + handle)
        self.handles.remove(handle)  # Double close is a test failure.
    def eject(self):
        assert not self.handles, self.handles
        self.action("eject")
    def record(self, event, result): self.action("record:" + event)


class ProtocolTests(unittest.TestCase):
    def test_success_order_and_no_physical_claim(self):
        b = FakeBackend()
        result = run(b)
        self.assertTrue(result.ejected)
        self.assertTrue(result.sleep_accepted)
        self.assertFalse(result.physical_stop_verified)
        self.assertLess(b.calls.index("lock:folder-mounted-volume"), b.calls.index("dismount:E"))
        self.assertLess(b.calls.index("verify_offline"), b.calls.index("sleep"))
        self.assertLess(b.calls.index("flush_disk"), b.calls.index("sleep"))
        self.assertLess(b.calls.index("close:folder-mounted-volume"), b.calls.index("sleep"))
        self.assertLess(b.calls.index("close:disk"), b.calls.index("eject"))
        self.assertFalse(b.handles)

    def test_preconditions_and_busy_volumes_never_sleep(self):
        for failure in ["validate", "open_disk", "open:E", "lock:E",
                        "open:folder-mounted-volume", "lock:folder-mounted-volume",
                        "flush:E", "dismount:E", "flush:folder-mounted-volume",
                        "dismount:folder-mounted-volume"]:
            with self.subTest(failure=failure):
                b = FakeBackend(failure)
                r = run(b)
                self.assertEqual(r.state, "unchanged")
                self.assertNotIn("sleep", b.calls)
                self.assertNotIn("offline", b.calls)
                self.assertFalse(b.handles)

    def test_partial_lock_does_not_dismount(self):
        b = FakeBackend("lock:folder-mounted-volume")
        run(b)
        self.assertFalse(any(c.startswith("dismount") for c in b.calls))

    def test_pre_sleep_failure_rolls_back(self):
        for failure in ["offline", "verify_offline", "flush_disk"]:
            with self.subTest(failure=failure):
                b = FakeBackend(failure)
                r = run(b)
                self.assertEqual(r.state, "restored_online")
                self.assertFalse(b.offline_state)
                self.assertNotIn("sleep", b.calls)
                self.assertFalse(b.handles)

    def test_sleep_failure_never_remounts_or_ejects(self):
        b = FakeBackend("sleep")
        r = run(b)
        self.assertEqual(r.state, "sleep_unknown_offline")
        self.assertTrue(r.sleep_attempted)
        self.assertFalse(r.sleep_accepted)
        self.assertTrue(b.offline_state)
        self.assertNotIn("online", b.calls)
        self.assertNotIn("eject", b.calls)
        self.assertEqual(b.calls.count("sleep"), 1)
        self.assertFalse(b.handles)

    def test_veto_keeps_disk_isolated_and_does_not_retry(self):
        b = FakeBackend("eject")
        r = run(b)
        self.assertEqual(r.state, "sleep_accepted_offline")
        self.assertFalse(r.ejected)
        self.assertNotIn("online", b.calls)
        self.assertEqual(b.calls.count("eject"), 1)
        self.assertFalse(b.handles)

    def test_rollback_failure_is_explicit(self):
        b = FakeBackend("flush_disk")
        original = b.offline
        def offline(disk, value):
            if not value:
                raise OSError("rollback")
            original(disk, value)
        b.offline = offline
        r = run(b)
        self.assertEqual(r.state, "recovery_required")
        self.assertIn("rollback", r.error)
        self.assertFalse(b.handles)

    def test_volume_close_failure_is_retained_for_cleanup(self):
        b = FakeBackend()
        original = b.close
        failed = False
        def close(handle):
            nonlocal failed
            if handle == "folder-mounted-volume" and not failed:
                failed = True
                raise OSError("close failed before sleep")
            original(handle)
        b.close = close
        r = run(b)
        self.assertEqual(r.state, "restored_online")
        self.assertNotIn("sleep", b.calls)
        self.assertFalse(b.handles)

    def test_disk_close_failure_never_starts_pnp_with_open_handle(self):
        b = FakeBackend()
        original = b.close
        failed = False
        def close(handle):
            nonlocal failed
            if handle == "disk" and not failed:
                failed = True
                raise OSError("close failed after sleep")
            original(handle)
        b.close = close
        r = run(b)
        self.assertEqual(r.state, "sleep_accepted_offline")
        self.assertNotIn("eject", b.calls)
        self.assertNotIn("online", b.calls)
        self.assertFalse(b.handles)


if __name__ == "__main__":
    unittest.main(verbosity=2)
