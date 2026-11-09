from __future__ import annotations

import subprocess
import sys

from fairwitness import __version__, chaos, cli, demo


def quiet(_: str) -> None:
    pass


def test_module_entrypoint_reports_the_new_command_name():
    result = subprocess.run(
        [sys.executable, "-m", "fairwitness", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == f"fairwitness {__version__}"


def test_demo_tells_the_story():
    res = demo.run(out=quiet)

    wrong = res["wrong_refund"]
    for name in ("baseline", "snapshot_review"):
        assert wrong[name]["outcome"].status == "committed"
        assert wrong[name]["violations"].refund_after_dispute == 1
    for name in ("plain_txn", "fairwitness"):
        assert wrong[name]["outcome"].reason == "order_disputed"
        assert wrong[name]["violations"].total == 0
    assert wrong["fairwitness_strict"]["outcome"].reason == "stale_evidence"

    worlds = res["both_worlds"]
    assert not worlds["baseline"]["B"]["correct"] and worlds["baseline"]["A"]["correct"]
    assert all(worlds[n][w]["correct"] for n in ("plain_txn", "fairwitness") for w in "AB")

    policy = res["policy_change"]
    assert policy["baseline"]["violations"].policy_cap_breaches == 1
    assert policy["fairwitness"]["outcome"].reason == "exceeds_policy_cap"

    drift = res["irrelevant_drift"]
    assert drift["fairwitness"].status == "committed" and drift["fairwitness"].drift
    assert drift["fairwitness_strict"].reason == "stale_evidence"

    retry = res["retry"]
    assert retry["again"].replayed and retry["refund_rows"] == 1
    assert retry["conflict"] is not None


def test_plans_are_reproducible():
    assert chaos.make_plan(42) == chaos.make_plan(42)
    assert chaos.make_plan(42) != chaos.make_plan(43)


def test_chaos_separates_the_strategies():
    s = chaos.simulate(runs=6, seed=100)
    assert s["baseline"].violations > 0
    assert s["snapshot_review"].violations == s["baseline"].violations
    for name in ("plain_txn", "fairwitness", "fairwitness_strict"):
        assert s[name].violations == 0, name
    # FairWitness makes exactly the decisions a correct hand-written transaction makes.
    assert s["fairwitness"].committed == s["plain_txn"].committed
    assert s["fairwitness"].reasons == s["plain_txn"].reasons
    # Strict mode trades commits for protection against unencoded rules.
    assert s["fairwitness_strict"].committed <= s["fairwitness"].committed


def test_cli_commands(tmp_path, capsys):
    assert cli.main(["demo"]) == 0
    assert "wrong refund" in capsys.readouterr().out
    assert cli.main(["chaos", "--runs", "2", "--json"]) == 0
    assert '"baseline"' in capsys.readouterr().out
    assert cli.main(["chaos", "--runs", "1", "--strategies", "nope"]) == 2
