"""The driver lease travels in every request, and the run's last requests release it.

Measured 2026-09-07 08:23 on NinjaTrader 8.1.8.2 (bridge.log against NinjaTrader's own
trace): the AddOn started the 120 s lease at the RECEIPT of the connect request, the
connect took 282 s, and on its next poll tick the AddOn found the lease expired and
tore down the connection it had just built. The AddOn now counts from the moment it
answered; this file pins the driver's half of the contract - what it sends.
"""
import json
import threading
import time

import pytest

from nt8bridge import playback_run as pr


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    trig = tmp_path / "trigger"
    res = tmp_path / "result"
    trig.mkdir()
    res.mkdir()
    monkeypatch.setattr(pr, "TRIG", trig)
    monkeypatch.setattr(pr, "RES", res)
    monkeypatch.setitem(pr._UI, "user_mode", False)
    monkeypatch.setitem(pr._shots, "dir", None)
    return trig, res


def answer_first_trigger(trig, res, seen: list) -> threading.Thread:
    """A stand-in AddOn: reads the first trigger file, records it, answers it."""

    def run():
        for _ in range(500):
            files = list(trig.iterdir())
            if files:
                f = files[0]
                req = json.loads(f.read_text(encoding="utf-8"))
                seen.append(req)
                f.unlink()
                (res / f.name).write_text(json.dumps(
                    {"id": req["id"], "status": "ok", "stage": req.get("stage"),
                     "steps": [{"step": "answered", "ok": True, "detail": ""}]}),
                    encoding="utf-8")
                return
            time.sleep(0.01)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_stage_sends_the_default_lease(bridge):
    trig, res = bridge
    seen = []
    t = answer_first_trigger(trig, res, seen)
    d = pr.stage("x", {"stage": "transport"}, wait=2, show=False)
    t.join(2)
    assert d.get("status") == "ok"
    assert seen and seen[0]["leaseSec"] == 120
    assert seen[0]["ttlSec"] == 2
    assert seen[0]["kind"] == "playbackrun"


def test_stage_lease_zero_is_the_clean_exit(bridge):
    """lease=0 is the AddOn's release (NoteLease: v <= 0 -> nothing to guard).
    The restore stage and the bot-output fetch after it send it."""
    trig, res = bridge
    seen = []
    t = answer_first_trigger(trig, res, seen)
    d = pr.stage("x", {"stage": "restore"}, wait=2, show=False, lease=0)
    t.join(2)
    assert d.get("status") == "ok"
    assert seen and seen[0]["leaseSec"] == 0


def test_teardown_requests_release_the_lease(bridge, monkeypatch):
    """restore_baseline() and the bot-output fetch after it both pass lease=0 -
    the run's last two requests. Anything else sending 120 after them would
    re-arm the lease and the AddOn would restore the baseline a second time
    two minutes later (bridge.log 2026-09-07 06:26:06Z, 06:24:01Z)."""
    calls = []

    def fake_stage(title, req, wait=None, show=True, report_miss=True, lease=120):
        calls.append({"title": title, "stage": req.get("stage"), "lease": lease})
        return {"status": "ok", "steps": [{"step": "ok", "ok": True, "detail": ""}]}

    monkeypatch.setattr(pr, "stage", fake_stage)
    pr.restore_baseline("Bot")
    pr.print_bot_output(lease=0)
    assert [c["stage"] for c in calls] == ["restore", "botout"]
    assert [c["lease"] for c in calls] == [0, 0]
    # and the same fetch OUTSIDE a teardown keeps the guard armed
    calls.clear()
    pr.print_bot_output()
    assert calls == [{"title": "botout", "stage": "botout", "lease": 120}]
