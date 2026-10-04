from falcon.harness import run_bulk
from falcon.metrics import metrics_from_events
from falcon.pdl.bitmap import get_bit, set_bit, slide_while_received


def test_bitmap_update_and_slide():
    b = 0
    b = set_bit(b, 0)
    b = set_bit(b, 2)
    assert get_bit(b, 0)
    assert not get_bit(b, 1)
    assert get_bit(b, 2)
    base, b2 = slide_while_received(10, b)
    assert base == 11
    assert get_bit(b2, 1)


def test_zero_loss_zero_retransmits():
    for tr in ("gbn", "sr", "falcon"):
        sim = run_bulk(tr, seed=1, n_packets=80, loss=0.0, reorder=0.0)
        m = metrics_from_events(sim.bus.events)
        assert m["packets_delivered"] == 80, (tr, m)
        assert m["retransmissions"] == 0, (tr, m)
        assert m["drops"] == 0, (tr, m)


def test_same_seed_identical_event_log():
    a = run_bulk("falcon", seed=9, n_packets=40, loss=0.01).bus.dumps()
    b = run_bulk("falcon", seed=9, n_packets=40, loss=0.01).bus.dumps()
    assert a == b


def test_falcon_beats_gbn_at_1pct_loss():
    n = 250
    gbn = metrics_from_events(run_bulk("gbn", seed=2, n_packets=n, loss=0.01, until=2.0).bus.events)
    fal = metrics_from_events(run_bulk("falcon", seed=2, n_packets=n, loss=0.01, until=2.0).bus.events)
    lossless = metrics_from_events(run_bulk("falcon", seed=2, n_packets=n, loss=0.0, until=2.0).bus.events)
    assert fal["goodput_bps"] > gbn["goodput_bps"]
    assert fal["goodput_bps"] > 0.5 * lossless["goodput_bps"]


def test_reorder_costs_falcon_far_fewer_retransmissions():
    """Under pure reordering the win is retransmission volume, not the spurious count.

    With zero loss, Go-Back-N's window retransmissions all target PSNs at or above the
    receiver's rcv_nxt, so none of them are spurious by the "already delivered" definition
    -- it just retransmits 20x more than it needs to. Asserting a spurious-retransmission
    gap here would compare nothing. Falcon-style's RACK/TLP never retransmits a PSN the
    receiver already has, which is what `spurious == 0` below actually certifies.
    """
    n = 120
    kwargs = dict(seed=3, n_packets=n, loss=0.0, reorder=0.3, until=2.0)
    gbn = metrics_from_events(run_bulk("gbn", **kwargs).bus.events)
    sr = metrics_from_events(run_bulk("sr", **kwargs).bus.events)
    fal = metrics_from_events(run_bulk("falcon", **kwargs).bus.events)

    for m in (gbn, sr, fal):
        assert m["packets_delivered"] == n, m
    assert fal["retransmissions"] < sr["retransmissions"] < gbn["retransmissions"]
    assert fal["spurious_retransmissions"] == 0
    assert fal["spurious_retransmissions"] <= gbn["spurious_retransmissions"]
    assert fal["goodput_bps"] > sr["goodput_bps"] > gbn["goodput_bps"]
