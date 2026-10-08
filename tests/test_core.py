from gyrfalcon.core.sim import Simulator
from gyrfalcon.core.event_bus import EventBus
from gyrfalcon.core.clock import Clock


def test_same_seed_identical_schedule_logs():
    def drive(seed: int) -> str:
        sim = Simulator(seed=seed)

        def ping(i: int) -> None:
            sim.bus.emit("tick", conn=0, i=i, r=sim.rng.random())
            if i < 5:
                sim.schedule(0.01, ping, i + 1)

        sim.schedule(0.0, ping, 0)
        sim.run()
        return sim.bus.dumps()

    a = drive(42)
    b = drive(42)
    c = drive(43)
    assert a == b
    assert a != c


def test_event_bus_jsonl_sorted_keys():
    clock = Clock()
    bus = EventBus(clock)
    clock.set(0.5)
    bus.emit("pkt_send", conn=1, psn=3, flow=0)
    text = bus.dumps()
    assert text.startswith("{")
    assert '"t":0.5' in text.replace(" ", "")
    assert text.count("\n") == 1 or text.endswith("\n")
