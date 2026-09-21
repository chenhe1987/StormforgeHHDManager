"""Service lifecycle regression tests; no real storage or SCM operations."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core import shutdown_service


class ShutdownTimingTests(unittest.TestCase):
    def setUp(self):
        class Framework:
            def __init__(self, args):
                self.ReportServiceStatus = Mock()

            def GetAcceptedControls(self):
                return 0x101

        self.events = SimpleNamespace(CreateEvent=Mock(return_value=1),
                                      SetEvent=Mock(), WaitForSingleObject=Mock(),
                                      INFINITE=-1)
        service = SimpleNamespace(SERVICE_ACCEPT_PRESHUTDOWN=0x100,
                                  SERVICE_ACCEPT_SHUTDOWN=4,
                                  SERVICE_STOP_PENDING=3, SERVICE_STOPPED=1)
        self.modules = patch.dict(sys.modules, {
            'win32event': self.events, 'win32service': service,
            'win32serviceutil': SimpleNamespace(ServiceFramework=Framework)})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.instance = shutdown_service._make_service_class()([])

    def test_preshutdown_requests_offline_and_sleep(self):
        controls = self.instance.GetAcceptedControls()
        self.assertTrue(controls & 4)
        self.assertTrue(controls & 0x100)
        self.instance.SvcOther(15)
        self.assertTrue(self.instance._park_requested)
        self.events.SetEvent.assert_called_once()

    def test_late_shutdown_runs_once_then_stops(self):
        self.instance.SvcShutdown()
        with patch.object(shutdown_service, '_park_all_disks') as park:
            self.instance.SvcDoRun()
            park.assert_called_once_with()
        self.instance.ReportServiceStatus.assert_called_with(1)

    def test_manual_stop_never_sleeps_disks(self):
        self.instance.SvcStop()
        with patch.object(shutdown_service, '_park_all_disks') as park:
            self.instance.SvcDoRun()
            park.assert_not_called()


if __name__ == '__main__':
    unittest.main()
