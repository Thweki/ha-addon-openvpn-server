import io
import json
import shutil
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock, patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.history import StatusHistory
from openvpn_addon.main import OpenVpnSupervisor, start_panel
from openvpn_addon.webui import PAGE, PanelActions, _build_handler


def status(clients=(), errors=()):
    return {
        "clients_online": list(clients),
        "health": {"checks": [
            {"name": name, "status": "error", "detail": "private diagnostic detail"}
            for name in errors
        ]},
    }


class HistoryTests(unittest.TestCase):
    def test_observations_deduplicate_and_record_recurrence(self):
        history = StatusHistory()
        first = history.collect(lambda: status(["alice"], ["crl"]))
        self.assertEqual(2, len(first["recent_status"]))
        self.assertNotIn("private diagnostic detail", json.dumps(first["recent_status"]))
        self.assertEqual(first, history.collect(lambda: status(["alice"], ["crl"])))
        history.collect(lambda: status())
        self.assertEqual([
            "Health check no longer reports an error: crl",
            "Client observed offline: alice",
        ], [event["message"] for event in history.snapshot()[:2]])
        history.collect(lambda: status(["alice"], ["crl"]))
        self.assertEqual(6, len(history.snapshot()))
        self.assertEqual("error", history.snapshot()[0]["kind"])

    def test_bounded_newest_first_and_defensive_snapshot(self):
        history = StatusHistory()
        for number in range(40):
            history.record("start", str(number))
        events = history.snapshot()
        self.assertEqual([str(n) for n in range(39, 9, -1)], [e["message"] for e in events])
        self.assertIsNotNone(datetime.fromisoformat(events[0]["at"]).tzinfo)
        events[0]["message"] = "changed"
        self.assertEqual("39", history.snapshot()[0]["message"])
        history.record("client", "x" * 1000)
        self.assertEqual(256, len(history.snapshot()[0]["message"]))
        self.assertEqual([], StatusHistory().snapshot())

    def test_failed_sample_preserves_previous_observations(self):
        history = StatusHistory()
        history.collect(lambda: status(["alice"]))
        before = history.snapshot()
        with self.assertRaises(OSError):
            history.collect(Mock(side_effect=OSError("unreadable")))
        self.assertEqual(before, history.snapshot())
        history.collect(lambda: status(["alice"]))
        self.assertEqual(before, history.snapshot())

    def test_concurrent_readers_do_not_duplicate_events(self):
        history = StatusHistory()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: history.collect(lambda: status(["alice"], ["crl"])), range(32)))
        self.assertTrue(all(len(result["recent_status"]) == 2 for result in results))
        self.assertEqual(2, len(history.snapshot()))

    def test_warning_is_recovery_from_error_without_recording_details(self):
        history = StatusHistory()
        history.collect(lambda: status(errors=["crl"]))
        sample = status(errors=["crl"])
        sample["health"]["checks"][0]["status"] = "warn"
        history.collect(lambda: sample)
        self.assertEqual("health", history.snapshot()[0]["kind"])
        history.collect(lambda: sample)
        self.assertEqual(2, len(history.snapshot()))


class SupervisorHistoryTests(unittest.TestCase):
    def setUp(self):
        self.supervisor = OpenVpnSupervisor(AddonConfig())
        self.process = Mock(returncode=None)
        self.process.poll.side_effect = lambda: self.process.returncode
        self.process.wait.side_effect = self.finish
        self.popen = patch("openvpn_addon.main.subprocess.Popen", return_value=self.process).start()
        patch("openvpn_addon.main.threading.Thread").start()
        self.addCleanup(patch.stopall)

    def finish(self, **kwargs):
        self.process.returncode = -15
        return -15

    def test_start_stop_and_repeated_calls(self):
        self.supervisor.start()
        self.supervisor.start()
        self.supervisor.stop()
        self.supervisor.stop()
        self.assertEqual(["stop", "start"], [e["kind"] for e in self.supervisor.history.snapshot()])
        self.assertEqual(1, self.popen.call_count)
        self.process.terminate.assert_called_once()
        # The supervisor wait loop may observe the same exit after stop().
        self.supervisor._record_exit(self.process, -15)
        self.assertEqual(2, len(self.supervisor.history.snapshot()))

    def test_manual_restart_records_both_transitions(self):
        self.supervisor.start()
        replacement = Mock(returncode=None)
        replacement.poll.return_value = None
        self.popen.return_value = replacement
        self.supervisor.restart("manual request")
        self.assertEqual(["start", "stop", "start"], [e["kind"] for e in self.supervisor.history.snapshot()])

    def test_launch_failure_records_safe_error_and_preserves_exception(self):
        self.popen.side_effect = OSError("sensitive exception text")
        with self.assertRaises(OSError):
            self.supervisor.start()
        events = self.supervisor.history.snapshot()
        self.assertEqual(1, len(events))
        self.assertEqual("error", events[0]["kind"])
        self.assertNotIn("sensitive", json.dumps(events))

    def test_unexpected_exit_recorded_without_panel_requests(self):
        stop = threading.Event()
        def finish():
            self.process.returncode = 2
            return 2
        self.process.wait.side_effect = finish
        with patch.object(stop, "wait", side_effect=lambda delay: stop.set()):
            self.supervisor.supervise_forever(stop)
        self.assertEqual("OpenVPN process exited (code 2)", self.supervisor.history.snapshot()[0]["message"])
        self.assertEqual("error", self.supervisor.history.snapshot()[0]["kind"])
        self.supervisor.stop()
        self.assertEqual(2, len(self.supervisor.history.snapshot()))

    def test_forced_stop_records_once(self):
        self.supervisor.start()
        self.process.wait.side_effect = [subprocess.TimeoutExpired("openvpn", 10), -9]
        self.process.kill.side_effect = lambda: setattr(self.process, "returncode", -9)
        self.supervisor.stop()
        self.process.kill.assert_called_once()
        self.assertEqual(["stop", "start"], [e["kind"] for e in self.supervisor.history.snapshot()])

    def test_exit_before_restart_is_recorded_only_once(self):
        self.supervisor.start()
        self.process.returncode = 0
        replacement = Mock(returncode=None)
        replacement.poll.return_value = None
        self.popen.return_value = replacement
        self.supervisor.start()
        # A delayed wait result for the old process must not add an old exit
        # after the new process's start event.
        self.supervisor._record_exit(self.process, 0)
        events = self.supervisor.history.snapshot()
        self.assertEqual(["start", "stop", "start"], [e["kind"] for e in events])
        self.assertEqual("OpenVPN process exited (code 0)", events[1]["message"])

    def test_panel_listeners_share_supervisor_history(self):
        with patch("openvpn_addon.main.panel_bind_addresses", return_value=["127.0.0.1", "127.0.0.2"]), patch("openvpn_addon.main.PanelServer") as server:
            start_panel(self.supervisor.config, Mock(), self.supervisor, threading.Event())
        self.assertEqual(2, server.call_count)
        for call in server.call_args_list:
            self.assertIs(self.supervisor.history, call.args[1].history)


class PanelHistoryTests(unittest.TestCase):
    def test_status_endpoint_keeps_history_across_requests_without_sockets(self):
        actions = PanelActions(*(Mock() for _ in range(6)))
        actions.history.record("start", "OpenVPN process started")
        handler = _build_handler(AddonConfig(), actions)
        class Connection:
            def makefile(self, mode, *args):
                return io.BytesIO(b"GET /api/status HTTP/1.0\r\n\r\n")
            def sendall(self, data):
                self.response += data
            response = b""
        def request(sample):
            connection = Connection()
            with patch("openvpn_addon.webui.collect_status", return_value=sample):
                handler(connection, ("127.0.0.1", 12345), Mock())
            return json.loads(connection.response.split(b"\r\n\r\n", 1)[1])
        first = request(status(["alice"]))
        second = request(status(["alice"]))
        self.assertEqual(first["recent_status"], second["recent_status"])
        self.assertEqual(["client", "start"], [e["kind"] for e in second["recent_status"]])
        self.assertEqual("Client observed offline: alice", request(status())["recent_status"][0]["message"])

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable for panel rendering test")
    def test_history_renderer_uses_text_nodes_and_handles_empty_state(self):
        script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
const elements = {};
function element(){
  return {children: [], append(...items){this.children.push(...items)},
    replaceChildren(...items){this.children = items},
    set innerHTML(value){throw new Error('history must not parse HTML')}};
}
global.document = {
  hidden: true,
  getElementById(id){return elements[id] ||= element()},
  createElement: element,
  createTextNode(text){return {textContent: text}},
  addEventListener(){},
};
global.fetch = () => new Promise(() => {});
global.setInterval = () => {};
'''
        assertions = r'''
renderHistory([]);
assert.equal(elements.history.children[0].textContent, 'No recent events.');
const message = '<img src=x onerror=alert(1)>';
renderHistory([{at:'2026-11-15T12:00:00+00:00', kind:'error', message}]);
const row = elements.history.children[0];
assert.equal(row.children[1].textContent, ' — ' + message);
assert.equal(row.children[0].dateTime, '2026-11-15T12:00:00+00:00');
assert.equal(row.className, 'err');
'''
        subprocess.run(["node", "-e", harness + script + assertions], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
