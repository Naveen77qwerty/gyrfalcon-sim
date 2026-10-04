from falcon.pdl.bitmap import get_bit, set_bit
from falcon.pdl.reliability import PdlSender
from falcon.core.sim import Simulator
from falcon.fae.policy import Policy
from falcon.net.path import Path
from falcon.net.packet import Packet


def test_rack_ignores_newer_transmits_and_received():
    sim = Simulator(seed=1)
    path = Path(sim, delay=1e-9, loss=0.0)
    path.attach(lambda p: None)
    pol = Policy(rack_rto=0.001)
    s = PdlSender(sim, path, conn=1, policy=pol)
    from falcon.pdl.reliability import Outstanding

    # PSN 1 was sent at t=0 and is eligible: sent before xmit_ts, not marked received.
    # PSN 2 was sent at t=0.5, after xmit_ts=0.1, so recovery must wait for it.
    s.outstanding[1] = Outstanding(1, 0, xmit_ts=0.0, payload=b"a", size=10, rsn=None, is_response=False)
    s.outstanding[2] = Outstanding(2, 0, xmit_ts=0.5, payload=b"b", size=10, rsn=None, is_response=False)
    s.unacked_per_flow[0] = 2
    fired: list[int] = []
    orig = s._transmit

    def wrap(psn, *a, **k):
        fired.append(psn)
        orig(psn, *a, **k)

    s._transmit = wrap  # type: ignore[method-assign]
    s._rack(now=0.01, xmit_ts=0.1, base_psn=1)
    assert 1 in fired
    assert 2 not in fired


def test_rack_skips_packets_marked_received():
    sim = Simulator(seed=1)
    path = Path(sim, delay=1e-9, loss=0.0)
    path.attach(lambda p: None)
    s = PdlSender(sim, path, conn=1, policy=Policy(rack_rto=0.001))
    from falcon.pdl.reliability import Outstanding

    o = Outstanding(1, 0, xmit_ts=0.0, payload=b"a", size=10, rsn=None, is_response=False)
    o.acked = True
    s.outstanding[1] = o
    fired: list[int] = []
    orig = s._transmit

    def wrap(psn, *a, **k):
        fired.append(psn)
        orig(psn, *a, **k)

    s._transmit = wrap  # type: ignore[method-assign]
    s._rack(now=0.01, xmit_ts=0.1, base_psn=1)
    assert fired == []


def test_rack_ignores_psns_outside_the_bitmap_range():
    sim = Simulator(seed=1)
    path = Path(sim, delay=1e-9, loss=0.0)
    path.attach(lambda p: None)
    s = PdlSender(sim, path, conn=1, policy=Policy(rack_rto=0.001))
    from falcon.pdl.reliability import Outstanding

    # base_psn=1 with a 128-bit bitmap reaches PSN 128 inclusive; 200 is outside it.
    s.outstanding[1] = Outstanding(1, 0, xmit_ts=0.0, payload=b"a", size=10, rsn=None, is_response=False)
    s.outstanding[200] = Outstanding(200, 0, xmit_ts=0.0, payload=b"b", size=10, rsn=None, is_response=False)
    fired: list[int] = []
    orig = s._transmit

    def wrap(psn, *a, **k):
        fired.append(psn)
        orig(psn, *a, **k)

    s._transmit = wrap  # type: ignore[method-assign]
    s._rack(now=0.01, xmit_ts=0.1, base_psn=1)
    assert fired == [1]


def test_tlp_lowest_unacked(monkeypatch):
    sim = Simulator(seed=1)
    sent: list[int] = []
    path = Path(sim, delay=1.0, loss=0.0)
    path.attach(lambda p: sent.append(p.psn))
    s = PdlSender(sim, path, conn=1, policy=Policy(tlp_idle=0.01))
    from falcon.pdl.reliability import Outstanding

    s.outstanding[4] = Outstanding(4, 0, 0.0, b"x", 10, None, False)
    s.outstanding[7] = Outstanding(7, 0, 0.0, b"y", 10, None, False)
    s.unacked_per_flow[0] = 2
    s._tlp_gen = 5
    s._tlp(5)
    sim.run(until=0.05)
    assert any(e["type"] == "tlp_fire" and e["psn"] == 4 for e in sim.bus.events)
