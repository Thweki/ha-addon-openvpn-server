import argparse
import threading
import unittest
from unittest.mock import Mock, call, patch

from openvpn_addon import main
from openvpn_addon.config import AddonConfig


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.config = AddonConfig()
        self.supervisor = main.OpenVpnSupervisor(self.config)
        self.stop_event = threading.Event()
        self.now = 100.0
        self.delays = []
        self.popen = self.enterContext(patch.object(main.subprocess, "Popen"))
        self.thread = self.enterContext(patch.object(main.threading, "Thread"))
        self.enterContext(patch.object(main.time, "time", side_effect=lambda: self.now))
        self.output = self.enterContext(patch("builtins.print"))

    def stop_after_retries(self, count):
        def wait(delay):
            self.delays.append(delay)
            if len(self.delays) == count:
                self.stop_event.set()
            return self.stop_event.is_set()

        self.enterContext(patch.object(self.stop_event, "wait", side_effect=wait))

    def process(self, uptime, exit_code, shutdown=False):
        process = Mock()
        process.poll.return_value = None

        def wait():
            self.now += uptime
            process.poll.return_value = exit_code
            if shutdown:
                self.stop_event.set()
            return exit_code

        process.wait.side_effect = wait
        return process

    def test_launch_errors_retry_with_capped_backoff_until_shutdown(self):
        self.popen.side_effect = [
            FileNotFoundError("openvpn missing"),
            PermissionError("openvpn not executable"),
            BlockingIOError("temporarily unable to fork"),
        ] + [OSError("launch failed")] * 4
        self.stop_after_retries(7)

        self.supervisor.supervise_forever(self.stop_event)

        self.assertEqual([2, 4, 8, 16, 32, 60, 60], self.delays)
        self.assertEqual(7, self.popen.call_count)
        self.thread.assert_not_called()
        self.assertFalse(self.supervisor.is_running())
        self.assertIsNone(self.supervisor.pid)
        self.assertIsNone(self.supervisor.last_exit_code)
        self.assertEqual(7, self.supervisor.restarts)
        self.assertTrue(any("openvpn missing" in str(item) for item in self.output.call_args_list))

    def test_successful_launch_after_failure_resets_backoff_after_stable_run(self):
        process = self.process(main.OPENVPN_FAST_EXIT_SECONDS, 3)
        self.popen.side_effect = [OSError("launch failed"), process, OSError("launch failed")]
        self.stop_after_retries(3)

        self.supervisor.supervise_forever(self.stop_event)

        self.assertEqual([2, 1, 2], self.delays)
        self.assertEqual(3, self.popen.call_count)
        process.wait.assert_called_once_with()
        self.thread.return_value.start.assert_called_once_with()
        self.assertEqual(3, self.supervisor.last_exit_code)
        self.assertEqual(3, self.supervisor.restarts)

    def test_fast_exits_and_launch_failures_share_backoff(self):
        self.popen.side_effect = [self.process(1, 1), OSError("launch failed"), self.process(1, 2)]
        self.stop_after_retries(3)

        self.supervisor.supervise_forever(self.stop_event)

        self.assertEqual([2, 4, 8], self.delays)
        self.assertEqual(2, self.supervisor.last_exit_code)
        self.assertEqual(3, self.supervisor.restarts)

    def test_shutdown_during_failed_launch_does_not_schedule_retry(self):
        def fail_and_stop(*args, **kwargs):
            self.stop_event.set()
            raise OSError("launch failed")

        self.popen.side_effect = fail_and_stop
        self.stop_after_retries(1)

        self.supervisor.supervise_forever(self.stop_event)

        self.popen.assert_called_once()
        self.assertEqual([], self.delays)
        self.assertEqual(0, self.supervisor.restarts)

    def test_shutdown_during_child_wait_does_not_schedule_retry(self):
        self.popen.return_value = self.process(1, 0, shutdown=True)
        self.stop_after_retries(1)

        self.supervisor.supervise_forever(self.stop_event)

        self.popen.assert_called_once()
        self.assertEqual([], self.delays)
        self.assertEqual(0, self.supervisor.restarts)

    def test_already_stopped_does_not_launch(self):
        self.stop_event.set()
        self.supervisor.supervise_forever(self.stop_event)
        self.popen.assert_not_called()

    def test_programming_errors_are_not_retried(self):
        self.popen.side_effect = ValueError("invalid process arguments")
        with self.assertRaisesRegex(ValueError, "invalid process arguments"):
            self.supervisor.supervise_forever(self.stop_event)
        self.popen.assert_called_once()

    def test_run_keeps_panel_available_during_launch_retry(self):
        # Exercise the real supervision loop while isolating PKI, files, networking,
        # signals and background services from the test host.
        self.enterContext(patch.object(main, "load_and_validate", return_value=self.config))
        for name in (
            "ensure_dirs", "PkiManager", "sync_client_ips", "render_server_config",
            "log_routing_intent", "export_default_client", "process_pending_requests",
        ):
            self.enterContext(patch.object(main, name))
        self.enterContext(patch.object(main.Path, "write_text"))
        self.enterContext(patch.object(main, "apply_network", return_value=Mock(warnings=[])))
        remove_network = self.enterContext(patch.object(main, "remove_network"))
        self.enterContext(patch.object(main.signal, "signal"))
        self.enterContext(patch.object(main.threading, "Event", return_value=self.stop_event))
        panel = Mock()
        self.enterContext(patch.object(main, "start_panel", return_value=[panel]))
        watcher = self.enterContext(patch.object(main, "RequestWatcher")).return_value
        self.popen.side_effect = FileNotFoundError("openvpn missing")

        def shutdown_after_checking_panel(delay):
            self.assertEqual(2, delay)
            panel.stop.assert_not_called()
            watcher.stop.assert_not_called()
            remove_network.assert_not_called()
            self.stop_event.set()
            return True

        wait = self.enterContext(patch.object(self.stop_event, "wait", side_effect=shutdown_after_checking_panel))

        self.assertEqual(0, main.cmd_run(argparse.Namespace()))

        self.assertEqual([call(2)], wait.call_args_list)
        self.popen.assert_called_once()
        panel.stop.assert_called_once_with()
        watcher.stop.assert_called_once_with()
        remove_network.assert_called_once_with(self.config)


if __name__ == "__main__":
    unittest.main()
