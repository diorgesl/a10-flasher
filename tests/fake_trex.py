"""Fake do TRexClient para testes de worker/controller (sem TRex real)."""


class FakeTRexClient:
    """Espelha a interface de a10flash.trex_client.TRexClient."""

    def __init__(self, path=None, daemon_args=("-i", "--astf"), port=4501,
                 astf_factory=None, popen=None, sleep=None):
        self.path = path
        self.daemon_args = daemon_args
        self.calls = []                  # ("start_daemon",), ("stats",), ...
        self.start_traffic_called = False
        self.cps_seen = None
        self.duration_seen = None
        self.profile_seen = None
        self.fail_stats = False          # stats() levanta TRexError
        self.daemon_fail = False         # start_daemon() levanta TRexError
        self.stats_dict = {"tx_bps": 2000, "rx_bps": 2000, "tx_pps": 100,
                           "rx_pps": 100, "active_sessions": 5,
                           "errors": 0}
        self.daemon_terminated = False
        self.started_daemon = False

    def start_daemon(self, timeout=60):
        self.calls.append(("start_daemon",))
        if self.daemon_fail:
            from a10flash.trex_client import TRexError
            raise TRexError("daemon não respondeu (fake)")
        self.started_daemon = True

    def stop_daemon(self):
        self.calls.append(("stop_daemon",))
        self.daemon_terminated = True

    def start_traffic(self, profile_path, cps, duration):
        self.calls.append(("start_traffic", profile_path, cps, duration))
        self.start_traffic_called = True
        self.cps_seen = cps
        self.duration_seen = duration
        self.profile_seen = profile_path

    def stats(self):
        self.calls.append(("stats",))
        if self.fail_stats:
            from a10flash.trex_client import TRexError
            raise TRexError("stats falhou (fake)")
        return dict(self.stats_dict)

    def stop_traffic(self):
        self.calls.append(("stop_traffic",))

    def stop_all(self):
        self.calls.append(("stop_all",))
        self.stop_traffic()
        self.stop_daemon()
