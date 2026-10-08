from gyrfalcon.core.sim import Simulator
from gyrfalcon.net.packet import Packet
from gyrfalcon.net.path import Path


def test_path_loss_rate_fixed_seed():
    sim = Simulator(seed=7)
    got: list[Packet] = []
    path = Path(sim, delay=0.001, loss=0.2, bandwidth_bps=1e12)
    path.attach(got.append)
    for i in range(200):
        path.send(Packet(conn=1, psn=i, size=100))
    sim.run()
    drops = [e for e in sim.bus.events if e["type"] == "pkt_drop"]
    assert 20 < len(drops) < 60
    assert len(got) + len(drops) == 200


def test_path_reorder_hold_back():
    sim = Simulator(seed=1)
    order: list[int] = []
    path = Path(sim, delay=0.001, reorder=1.0, reorder_delay=0.005, loss=0.0, bandwidth_bps=1e12)
    path.attach(lambda p: order.append(p.psn))
    path.send(Packet(conn=1, psn=0, size=100))
    path.send(Packet(conn=1, psn=1, size=100))
    sim.run()
    # both delayed equally + reorder extra on each (p=1), still ordered by send time
    assert order == [0, 1]
    assert any(e["type"] == "pkt_reorder" for e in sim.bus.events)


def test_path_kill_drops():
    sim = Simulator(seed=1)
    got: list[Packet] = []
    path = Path(sim, delay=0.001, loss=0.0)
    path.attach(got.append)
    path.kill()
    path.send(Packet(conn=1, psn=0, size=100))
    sim.run()
    assert got == []
    assert any(e["reason"] == "path_killed" for e in sim.bus.events if e["type"] == "pkt_drop")
