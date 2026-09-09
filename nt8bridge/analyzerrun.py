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

"""optimize / walkforward / multiobjective: NinjaTrader's own parameter search,
driven on the Strategy Analyzer tab exactly as the Run button drives it.

The three commands share ONE request kind, ``analyzerrun``, and ONE option
syntax with the headless runner (Nt8Cli), so a run can be moved between the two
hosts by changing nothing but the program name::

    python -m nt8bridge walkforward --template=<xml> --opt=Fast:20:30:5 [--anchored]
                                    [--optimizer=default|genetic] [--fitness=<name>]
                                    [--OptimizationPeriod=<days>] [--TestPeriod=<days>]
                                    [--out=<csv>] [--timeout=<s>]

* ``--template`` is the strategy template file NinjaTrader wrote itself (a full
  path, or a bare name resolved in the strategy's own template folder - the
  same rule as ``satemplate``). It carries the complete parameter set, the
  instrument and the window.
* ``--opt`` is ``Name:min:max:step[,...]``; every name must be a public
  property of the strategy. The AddOn writes them as NinjaTrader's own
  ``Parameter`` objects onto the template's ``OptimizationParameters`` - the
  collection the GUI's parameter grid fills - because NinjaTrader refuses a
  run without one (``The strategy must have at least one parameter to
  optimize``, measured on 8.1.8.2 with the type set and the collection empty).
* Any further ``--Name=value`` is written to the strategy property of that
  name after the template, so ``--OptimizationPeriod=10 --TestPeriod=5`` sets
  the walk-forward window lengths in days without editing the file.
* The AddOn sets the tab's ``BacktestType`` to the run mode and executes the
  view model's ``RunCommand``; NinjaTrader then does what it does for a click.
  The result is read off the tab's results grid once the run has finished:
  one summary row, one row per window (category WalkForward, the out-of-sample
  run) and one per combination (category Optimize under the window, the
  in-sample run ranked by its performance value), each with its parameters
  and performance, plus
  the CSV files the strategy wrote during the run (read from the strategy's
  ``LogModes`` folder when it has that property, counting only files with the
  ``LogFilePrefix`` the AddOn sets for the run when the strategy has one;
  ``--out`` copies them as
  ``<out>`` for one file and ``<out>_<k><ext>`` in creation order for several,
  which is what Nt8Cli does with the same option).

Wire format::

  request : {"id", "kind": "analyzerrun", "mode": "Optimize|WalkForward|
             WalkForwardAnchored|MultiObjective", "template", "opt",
             "optimizer", "fitness", "params": {...}, "timeoutSec", "ttlSec"}
  response: {"id", "status": "ok"|"error", "mode", "backtestType", "category",
             "template", "strategy", "instrument", "from", "to",
             "parameters": [{name, type, min, max, increment}], "optimizer",
             "fitness", "combinations", "elapsedSec", "rows": [...],
             "windows", "rankedWindows", "logDir", "csvFiles": [...],
             "errors": [{code, message}]}
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from nt8bridge import ntio
from nt8bridge.compile import new_request_id

# CLI command -> the value the AddOn writes to StrategyAnalyzerTabProperties.BacktestType
# (enum StrategyAnalyzerGuiBacktestType: Backtest, Optimize, WalkForward,
# WalkForwardAnchored, MultiObjective, AiGenerate - read off NinjaTrader.Gui 8.1.8.2).
MODES = {
    "optimize": "Optimize",
    "walkforward": "WalkForward",
    "multiobjective": "MultiObjective",
}
COMMANDS = tuple(MODES)
OPTIMIZERS = ("default", "genetic")
DEFAULT_TIMEOUT = 1800.0


def parse_opt_spec(spec: str) -> list[dict]:
    """Check the SHAPE of ``Name:min:max:step[,...]`` before the request leaves.

    Types are the AddOn's business (it knows the property), so min and max
    travel as text; only the field count and the step width are judged here,
    with the wording Nt8Cli uses for the same mistakes.
    """
    entries = []
    for part in [p for p in (spec or "").split(",") if p.strip()]:
        f = part.split(":")
        if len(f) != 4:
            raise ValueError("--opt entry '%s' needs 4 fields Name:min:max:step." % part)
        name = f[0].strip()
        if not re.match(r"^[A-Za-z_]\w*$", name):
            raise ValueError("--opt entry '%s': '%s' is not a property name." % (part, name))
        try:
            step = float(f[3].strip())
        except ValueError:
            raise ValueError("--opt entry '%s': step '%s' is not a number." % (part, f[3].strip()))
        if step <= 0:
            raise ValueError("step width in '%s' must be > 0." % part)
        entries.append({"name": name, "min": f[1].strip(), "max": f[2].strip(),
                        "step": f[3].strip()})
    if not entries:
        raise ValueError("needs --opt=Name:min:max:step[,...]")
    return entries


def property_overrides(extra: list[str]) -> dict:
    """Turn the unknown ``--Name=value`` tokens into strategy property writes.

    This is the rule the headless runner applies to every option it does not
    know itself; a token of any other shape is a typo, not an override.
    """
    out = {}
    for tok in extra:
        m = re.match(r"^--([A-Za-z_]\w*)=(.*)$", tok)
        if not m:
            raise ValueError("unexpected argument %r - a strategy property override is"
                             " written --Name=value" % tok)
        out[m.group(1)] = m.group(2)
    return out


def build_request(request_id: str, mode: str, template: str, opt: str, *,
                  optimizer: str = "default", fitness: str = "", anchored: bool = False,
                  params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(COMMANDS))
    if optimizer not in OPTIMIZERS:
        raise ValueError("--optimizer=%s is not a known optimizer. Valid: default | genetic."
                         % optimizer)
    backtest_type = MODES[mode]
    if anchored:
        if mode != "walkforward":
            raise ValueError("--anchored belongs to walkforward only")
        backtest_type = "WalkForwardAnchored"
    return {
        "id": request_id,
        "kind": "analyzerrun",
        "mode": backtest_type,
        "template": template,
        "opt": opt,
        "optimizer": optimizer,
        "fitness": fitness or "",
        "params": dict(params or {}),
        # The AddOn stops waiting for the grid when the client would have
        # stopped waiting for the file; one number, read on both sides.
        "timeoutSec": float(timeout),
        # A request that sat in the queue past this is not executed (the AddOn's
        # own rule); a parameter search is never worth running on a stale tab.
        "ttlSec": 300.0,
    }


def run_analyzer(mode: str, template: str, opt: str, *, optimizer: str = "default",
                 fitness: str = "", anchored: bool = False, params: dict | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Drop one analyzerrun request for the AddOn and wait for its result file."""
    trigger, result = ntio.ensure_bridge_dirs()
    rid = new_request_id()
    ntio.atomic_write_json(
        trigger / f"analyzerrun_{rid}.json",
        build_request(rid, mode, template, opt, optimizer=optimizer, fitness=fitness,
                      anchored=anchored, params=params, timeout=timeout),
    )
    # The result arrives only after the run; the client waits the run's own
    # budget plus a margin for the AddOn to write the file.
    return ntio.poll_for_json(result / f"analyzerrun_{rid}.json", timeout=timeout + 30.0)


def deliver_csvs(csv_files: list[str], out: str | None) -> list[str]:
    """Copy the run's CSV files to ``out``: one file takes that name, several
    become ``<out>_<k><ext>`` in the order the AddOn lists them (creation order).
    Returns the destination paths; without ``out`` the sources are returned as
    they are."""
    files = [str(f) for f in (csv_files or [])]
    if not out or not files:
        return files
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dests = []
    for k, src in enumerate(files):
        dest = out_path if len(files) == 1 else out_path.with_name(
            out_path.stem + "_%d" % k + out_path.suffix)
        shutil.copyfile(src, dest)
        dests.append(str(dest))
    return dests
