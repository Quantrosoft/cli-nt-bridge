# MIT License
# Copyright (c) 2026 Quantrosoft Pty. Ltd.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import json
from pathlib import Path

import pytest

from nt8bridge import analyzerrun, cli


def test_parse_opt_spec_accepts_the_headless_runner_syntax():
    entries = analyzerrun.parse_opt_spec("DeltaVolume1:150:250:50,Fast:20:30:5")
    assert [e["name"] for e in entries] == ["DeltaVolume1", "Fast"]
    assert entries[0] == {"name": "DeltaVolume1", "min": "150", "max": "250", "step": "50"}


@pytest.mark.parametrize("spec, fragment", [
    ("Fast:20:30", "needs 4 fields"),
    ("Fast:20:30:0", "must be > 0"),
    ("Fast:20:30:x", "is not a number"),
    ("", "needs --opt"),
    ("2Fast:1:2:1", "not a property name"),
])
def test_parse_opt_spec_refuses_the_same_mistakes_as_nt8cli(spec, fragment):
    with pytest.raises(ValueError) as e:
        analyzerrun.parse_opt_spec(spec)
    assert fragment in str(e.value)


def test_property_overrides_take_name_equals_value_only():
    assert analyzerrun.property_overrides(["--OptimizationPeriod=10", "--TestPeriod=5"]) == {
        "OptimizationPeriod": "10", "TestPeriod": "5"}
    with pytest.raises(ValueError):
        analyzerrun.property_overrides(["--TestPeriod", "5"])


def test_build_request_carries_mode_template_and_ranges():
    r = analyzerrun.build_request("rid1", "walkforward", r"C:\t\WFO.xml", "Fast:20:30:5",
                                  params={"TestPeriod": "5"}, timeout=900.0)
    assert r["kind"] == "analyzerrun"
    assert r["mode"] == "WalkForward"
    assert r["template"] == r"C:\t\WFO.xml"
    assert r["opt"] == "Fast:20:30:5"
    assert r["optimizer"] == "default"
    assert r["params"] == {"TestPeriod": "5"}
    assert r["timeoutSec"] == 900.0


def test_build_request_anchored_is_a_walk_forward_mode_of_its_own():
    r = analyzerrun.build_request("rid2", "walkforward", "WFO", "Fast:20:30:5", anchored=True)
    assert r["mode"] == "WalkForwardAnchored"
    with pytest.raises(ValueError):
        analyzerrun.build_request("rid3", "optimize", "WFO", "Fast:20:30:5", anchored=True)
    with pytest.raises(ValueError):
        analyzerrun.build_request("rid4", "optimize", "WFO", "Fast:20:30:5", optimizer="random")


def test_run_analyzer_writes_its_request_and_reads_its_own_result_file(monkeypatch):
    captured, polled = {}, {}
    monkeypatch.setattr(analyzerrun.ntio, "ensure_bridge_dirs", lambda: (Path("trig"), Path("res")))
    monkeypatch.setattr(analyzerrun, "new_request_id", lambda: "rid7")
    monkeypatch.setattr(analyzerrun.ntio, "atomic_write_json",
                        lambda p, o: captured.update(path=p, obj=o))
    monkeypatch.setattr(analyzerrun.ntio, "poll_for_json",
                        lambda p, timeout=30.0: polled.update(path=p, timeout=timeout)
                        or {"status": "ok"})
    out = analyzerrun.run_analyzer("optimize", "WFO", "Fast:20:30:5", timeout=600.0)
    assert captured["path"] == Path("trig") / "analyzerrun_rid7.json"
    assert captured["obj"]["mode"] == "Optimize"
    assert polled["path"] == Path("res") / "analyzerrun_rid7.json"
    # the client waits the run's budget plus the AddOn's margin to write the file
    assert polled["timeout"] == 630.0
    assert out["status"] == "ok"


def test_deliver_csvs_numbers_several_files_in_the_order_given(tmp_path):
    srcs = []
    for k in range(3):
        f = tmp_path / ("run_%d.csv" % k)
        f.write_text("row%d" % k)
        srcs.append(str(f))
    out = tmp_path / "out" / "wfo.csv"
    dests = analyzerrun.deliver_csvs(srcs, str(out))
    assert [Path(d).name for d in dests] == ["wfo_0.csv", "wfo_1.csv", "wfo_2.csv"]
    assert (tmp_path / "out" / "wfo_1.csv").read_text() == "row1"
    single = analyzerrun.deliver_csvs(srcs[:1], str(tmp_path / "one.csv"))
    assert single == [str(tmp_path / "one.csv")]
    assert analyzerrun.deliver_csvs(srcs, None) == srcs


def test_cli_walkforward_passes_overrides_and_anchored_through(monkeypatch, capsys):
    seen = {}

    def fake_run(mode, template, opt, **kw):
        seen.update(mode=mode, template=template, opt=opt, **kw)
        return {"status": "ok", "csvFiles": []}

    monkeypatch.setattr(cli.ntanalyzerrun, "run_analyzer", fake_run)
    rc = cli.main(["walkforward", "--template=WFO.xml", "--opt=Fast:20:30:5", "--anchored",
                   "--OptimizationPeriod=10", "--TestPeriod=5", "--timeout", "60"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["command"] == "walkforward"
    assert seen["mode"] == "walkforward"
    assert seen["template"] == "WFO.xml"
    assert seen["opt"] == "Fast:20:30:5"
    assert seen["anchored"] is True
    assert seen["params"] == {"OptimizationPeriod": "10", "TestPeriod": "5"}
    assert seen["timeout"] == 60.0


def test_cli_optimize_refuses_a_malformed_range_before_writing_a_request(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli.ntanalyzerrun, "run_analyzer",
                        lambda *a, **k: called.append(1) or {"status": "ok"})
    rc = cli.main(["optimize", "--template=WFO.xml", "--opt=Fast:20:30"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert "needs 4 fields" in payload["error"]
    assert called == []


def test_cli_optimize_refuses_an_override_of_the_wrong_shape(monkeypatch, capsys):
    monkeypatch.setattr(cli.ntanalyzerrun, "run_analyzer", lambda *a, **k: {"status": "ok"})
    rc = cli.main(["optimize", "--template=WFO.xml", "--opt=Fast:20:30:5", "--TestPeriod", "5"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert "--Name=value" in payload["error"]
