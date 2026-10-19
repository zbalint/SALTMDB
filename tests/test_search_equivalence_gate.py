"""Synthetic sensitivity and refusal checks for the offline equivalence gate."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from typing import Any, cast
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def comparer():
    return load_script("compare_search_equivalence")


@pytest.fixture
def runner():
    return load_script("search_equivalence_gate")


@pytest.fixture
def artifact():
    # JSON fixtures deliberately retain insertion order; no corpus text or real identifiers.
    return json.loads("""{
      "header": {"input_sha256":"input", "runner_sha256":"runner",
        "environment":{"PYTHONHASHSEED":"0"}, "model_sha256":{"model.onnx":"model"},
        "src_realpath":"/fiction/src/saltmdb/__init__.py", "src_head":"abc",
        "src_status_full":"", "src_status_src":""},
      "complete":true,
      "liveness":{"preflight":true,"cross_encoder":true,"preview":true,"semantic":true},
      "cases":[{"key":["fiction.json",0,"A/broad/ce-off"],
        "arguments":{"mode":"broad"},"environment":{},"elapsed_seconds":1.0,
        "output":{"results":[{"id":"first","score":0.123456789,"relevance_preview":"alpha"},
                              {"id":"second","score":0.1,"relevance_preview":"beta"}],
                  "next_cursor":"offset:2","diagnostics":{"candidates":[{"in_semantic":true}]}}},
        {"key":["fiction.json",0,"C/error/ce-off"],"arguments":{},"environment":{},
         "elapsed_seconds":1.0,"output":{"exception":{"type":"ValueError","message":"bad cap"}}}]
    }""")


def differences(comparer, baseline, candidate, **kwargs):
    return comparer.compare_artifacts(baseline, candidate, baseline_sha="abc", **kwargs)


def test_identical_outputs_and_latency_compare_equal(comparer, artifact):
    candidate = copy.deepcopy(artifact)
    candidate["cases"][0]["elapsed_seconds"] = 999
    assert differences(comparer, artifact, candidate) == []


@pytest.mark.parametrize(
    "change,expected_path",
    [
        ("swap", "results[0].id"),
        ("score", "results[0].score"),
        ("preview", "relevance_preview"),
        ("cursor", "next_cursor"),
        ("diagnostics", "diagnostics"),
        ("added", "results"),
        ("error", "exception.message"),
        ("key_order", "results[0]"),
    ],
)
def test_single_output_difference(comparer, artifact, change, expected_path):
    candidate = copy.deepcopy(artifact)
    output = candidate["cases"][0]["output"]
    if change == "swap":
        output["results"].reverse()
    elif change == "score":
        output["results"][0]["score"] = 0.123456788
    elif change == "preview":
        output["results"][0]["relevance_preview"] = "alphb"
    elif change == "cursor":
        output["next_cursor"] = "offset:3"
    elif change == "diagnostics":
        del output["diagnostics"]
    elif change == "added":
        output["results"].append({"id": "third"})
    elif change == "error":
        candidate["cases"][1]["output"]["exception"]["message"] = "different cap"
    else:
        item = output["results"][0]
        output["results"][0] = dict(reversed(list(item.items())))
    found = differences(comparer, artifact, candidate)
    assert len(found) == 1
    assert expected_path in found[0]


@pytest.mark.parametrize("field", ["input_sha256", "runner_sha256", "environment", "model_sha256"])
def test_header_difference(comparer, artifact, field):
    candidate = copy.deepcopy(artifact)
    if isinstance(candidate["header"][field], dict):
        key = next(iter(candidate["header"][field]))
        candidate["header"][field][key] = "changed"
    else:
        candidate["header"][field] = "changed"
    found = differences(comparer, artifact, candidate)
    assert len(found) == 1
    assert f"header.{field}" in found[0]


def test_allowlist_is_explicit_and_does_not_hide_other_keys(comparer, artifact):
    artifact["cases"][0]["output"]["synthetic_volatile"] = 1
    candidate = copy.deepcopy(artifact)
    candidate["cases"][0]["output"]["synthetic_volatile"] = 2
    assert differences(comparer, artifact, candidate)
    assert (
        differences(comparer, artifact, candidate, allowlist=frozenset({"synthetic_volatile"}))
        == []
    )
    candidate["cases"][0]["output"]["next_cursor"] = "offset:99"
    assert differences(comparer, artifact, candidate, allowlist=frozenset({"synthetic_volatile"}))


@pytest.mark.parametrize("change,word", [("missing", "missing case"), ("extra", "extra case")])
def test_case_set_difference(comparer, artifact, change, word):
    candidate = copy.deepcopy(artifact)
    if change == "missing":
        candidate["cases"].pop()
    else:
        extra = copy.deepcopy(candidate["cases"][0])
        extra["key"][1] = 99
        candidate["cases"].append(extra)
    found = differences(comparer, artifact, candidate)
    assert len(found) == 1
    assert word in found[0]


def test_provenance_can_differ_but_expect_src_must_match(comparer, artifact):
    candidate = copy.deepcopy(artifact)
    candidate["header"].update(
        src_realpath="/other/src/saltmdb/__init__.py",
        src_head="def",
        src_status_full="?? scripts/new.py",
        src_status_src=" M src/a.py",
    )
    assert differences(comparer, artifact, candidate, expect_src="/other/src") == []
    assert "expect-src" in differences(comparer, artifact, candidate, expect_src="/wrong/src")[0]


def test_baseline_src_status_is_rejected(comparer, artifact):
    baseline = copy.deepcopy(artifact)
    baseline["header"]["src_status_src"] = " M src/changed.py"
    assert "dirty baseline source tree under src" in differences(comparer, baseline, artifact)[0]


def test_baseline_full_status_is_provenance_only(comparer, artifact):
    baseline = copy.deepcopy(artifact)
    baseline["header"]["src_status_full"] = "?? scripts/new.py"
    assert differences(comparer, baseline, artifact) == []


def test_wrong_baseline_sha_is_rejected(comparer, artifact):
    assert (
        "baseline SHA does not match"
        in comparer.compare_artifacts(artifact, artifact, baseline_sha="wrong")[0]
    )


def test_tagless_derived_cases_have_unique_keys(runner):
    seed = {
        "status": "raw",
        "title": "title",
        "newest_titles": ["title"],
        "tags": [],
        "alias_tags": [],
        "memory_type": "fact",
        "is_core": False,
        "metadata": {},
    }
    cases = runner._derived_cases(seed, [("fixture.json", 0, "query")])
    keys = [tuple(case["key"]) for case in cases]
    assert len(keys) == len(set(keys))


def test_effective_case_arguments_are_unique_across_tiers(runner):
    cases = runner._cases([("fixture.json", 0, "fixture query")])
    effective = [
        (
            json.dumps(case["arguments"], sort_keys=True),
            json.dumps(case["environment"], sort_keys=True),
        )
        for case in cases
        if case["tier"] in {"A", "B"}
    ]
    assert len(effective) == len(set(effective))


def test_runner_rejects_unknown_search_keyword(runner):
    with pytest.raises(ValueError, match="unknown search_memory keyword"):
        runner.validate_arguments(lambda mode: None, {"unknown": True})


def test_runner_requires_pythonhashseed(runner, monkeypatch):
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    with pytest.raises(ValueError, match="PYTHONHASHSEED"):
        runner.check_runtime()


def test_runner_rejects_isolated_interpreter(runner, monkeypatch):
    flags = type("Flags", (), {"isolated": 1, "ignore_environment": 0})()
    monkeypatch.setattr(runner.sys, "flags", flags)
    with pytest.raises(ValueError, match="isolated"):
        runner.check_runtime()


def test_source_header_records_input_hash(runner, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src"))
    import saltmdb

    monkeypatch.setattr(runner, "_sha256", lambda path: "runner-hash")
    monkeypatch.setattr(runner, "_model_hashes", lambda path: {})
    monkeypatch.setattr(runner, "_package_versions", lambda: {})

    class Result:
        stdout = ""

    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: Result())
    source_root = Path(saltmdb.__file__).resolve().parents[1]
    header = runner._source_header(source_root, Path(runner.__file__), "input-hash")
    assert header["input_sha256"] == "input-hash"


def test_source_header_rejects_wrong_expect_src(runner, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src"))
    import saltmdb

    monkeypatch.setattr(runner, "_sha256", lambda path: "runner-hash")
    monkeypatch.setattr(runner, "_model_hashes", lambda path: {})
    monkeypatch.setattr(runner, "_package_versions", lambda: {})

    class Result:
        stdout = ""

    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: Result())
    wrong_root = tmp_path / "wrong-src"
    with pytest.raises(ValueError, match="does not match"):
        runner._source_header(wrong_root, Path(saltmdb.__file__), "input-hash")


@pytest.mark.parametrize("change", ["incomplete", "duplicate", "liveness", "arguments"])
def test_artifact_integrity_failures(comparer, artifact, change):
    candidate = copy.deepcopy(artifact)
    if change == "incomplete":
        candidate["complete"] = False
    elif change == "duplicate":
        candidate["cases"].append(copy.deepcopy(candidate["cases"][0]))
    elif change == "liveness":
        candidate["liveness"]["semantic"] = False
    else:
        candidate["cases"][0]["arguments"]["mode"] = "strict"
    assert differences(comparer, artifact, candidate)


@pytest.mark.parametrize("script", ["search_equivalence_gate", "compare_search_equivalence"])
@pytest.mark.parametrize("kind", ["live", "suffix", "directory", "live_subtree"])
def test_database_refusals(script, kind, tmp_path):
    module = load_script(script)
    live = tmp_path / "live.db"
    live.touch()
    source = tmp_path / "snapshot.txt"
    source.touch()
    paths = {
        "live": live,
        "suffix": source,
        "directory": tmp_path,
        "live_subtree": Path.home() / ".saltmdb" / "snapshot.db",
    }
    with pytest.raises(ValueError, match="Refusing"):
        module.validate_db_path(paths[kind], live_path=live)


def test_unknown_keyword_refused_before_search(runner):
    def search(*, query_keywords=None):
        return query_keywords

    with pytest.raises(ValueError, match="unknown.*use_cross_encoder"):
        runner.validate_arguments(search, {"query_keywords": "fiction", "use_cross_encoder": True})


def test_isolated_interpreter_refused():
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "search_equivalence_gate.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "isolated" in result.stderr


def test_missing_hash_seed_refused(runner, monkeypatch):
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    with pytest.raises(ValueError, match="PYTHONHASHSEED"):
        runner.check_runtime()


def test_failing_preflight_aborts(runner):
    with pytest.raises(ValueError, match="preflight"):
        runner.check_preflight(lambda **kwargs: {"ready": False, "scores": None})


@pytest.mark.parametrize("scores", [[1.0], [float("nan"), 1.0], [True, 1.0]])
def test_preflight_independently_checks_scores(runner, scores):
    with pytest.raises(ValueError, match="preflight"):
        runner.check_preflight(lambda **kwargs: {"ready": True, "scores": scores})


def test_comparer_cli_reports_unchanged_and_nonzero_difference(
    comparer, artifact, tmp_path, capsys
):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(artifact))
    candidate.write_text(json.dumps(artifact))
    args = ["--baseline", str(baseline), "--candidate", str(candidate), "--baseline-sha", "abc"]
    assert comparer.main(args) == 0
    assert "candidate tree is unchanged from baseline" in capsys.readouterr().out
    artifact["cases"][0]["output"]["next_cursor"] = "offset:9"
    candidate.write_text(json.dumps(artifact))
    assert comparer.main(args) == 1
    assert "next_cursor" in capsys.readouterr().out


def test_tier_b_effective_matrix_has_no_duplicate_argument_environment_pairs(runner):
    queries = [("fixture.json", 0, "fixture query")]
    cases = runner._cases(queries)
    effective = [
        (
            json.dumps(case["arguments"], sort_keys=True),
            json.dumps(case["environment"], sort_keys=True),
        )
        for case in cases
        if case["tier"] == "B"
    ]
    assert len(effective) == len(set(effective))


def test_dynamic_cases_depend_on_snapshot_seed_not_search_output(runner):
    seed = {
        "title": "fixture title",
        "tags": ["#fixture"],
        "memory_type": "fact",
        "agent_id": "agent",
        "context_id": "context",
        "agent_session_id": "session",
        "is_core": False,
        "metadata": {"project": "fixture"},
        "alias_tags": [],
    }
    queries = [("fixture.json", 0, "fixture query")]
    first = runner._derived_cases(seed, queries)
    seed["title"] = "changed title"
    second = runner._derived_cases(seed, queries)
    assert first != second
    assert all("output" not in case for case in first + second)


def test_comparer_distinguishes_bool_and_numeric(comparer, artifact):
    baseline = copy.deepcopy(artifact)
    candidate = copy.deepcopy(artifact)
    baseline["cases"][0]["output"]["results"][0]["score"] = 1
    candidate["cases"][0]["output"]["results"][0]["score"] = True
    found = differences(comparer, baseline, candidate)
    assert found and "score" in found[0]
    baseline["cases"][0]["output"]["results"][0]["score"] = 0
    candidate["cases"][0]["output"]["results"][0]["score"] = 0.0
    found = differences(comparer, baseline, candidate)
    assert found and "score" in found[0]


def test_incomplete_artifacts_are_rejected(comparer, artifact):
    baseline = copy.deepcopy(artifact)
    candidate = copy.deepcopy(artifact)
    baseline["complete"] = False
    candidate["complete"] = False
    assert differences(comparer, baseline, candidate)


@pytest.mark.parametrize("module_name", ["search_equivalence_gate", "compare_search_equivalence"])
def test_database_subtree_refusal(module_name, runner, comparer, tmp_path, monkeypatch):
    module = runner if module_name == "search_equivalence_gate" else comparer
    fake_home = tmp_path / "home"
    nested = fake_home / ".saltmdb" / "sub" / "snapshot.db"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"db")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    with pytest.raises(ValueError, match="Refusing"):
        module.validate_db_path(nested)


def test_liveness_evidence_is_shape_sensitive(runner):
    base = {"key": ["A", 0, "x"], "output": {"results": []}}
    assert runner._has_liveness([base]) == {
        "cross_encoder": False,
        "preview": False,
        "semantic": False,
    }
    evidence = copy.deepcopy(base)
    evidence["output"] = cast(
        Any,
        {
            "results": [{"relevance_preview": "x"}],
            "diagnostics": {
                "cross_encoder": {"executed": True},
                "candidates": [{"in_semantic": True}],
            },
        },
    )
    assert runner._has_liveness([evidence]) == {
        "cross_encoder": True,
        "preview": True,
        "semantic": True,
    }


def test_liveness_preview_only_shapes(runner):
    list_preview = {"output": {"results": [{"relevance_preview": "x"}]}}
    dict_preview = {"output": {"relevance_preview": "x"}}
    expected = {"cross_encoder": False, "preview": True, "semantic": False}
    assert runner._has_liveness([list_preview]) == expected
    assert runner._has_liveness([dict_preview]) == expected


@pytest.mark.parametrize(
    "field",
    [
        "input_sha256",
        "runner_sha256",
        "src_head",
        "environment",
        "model_sha256",
        "package_versions",
    ],
)
def test_resume_rejects_changed_identity_field(runner, tmp_path, field):
    destination = tmp_path / "partial.json"
    header = {
        "input_sha256": "one",
        "runner_sha256": "r",
        "src_head": "h",
        "environment": {},
        "model_sha256": {},
        "package_versions": {},
    }
    destination.write_text(json.dumps({"header": header, "cases": [{"key": ["A", 0, "x"]}]}))
    changed = dict(header, **{field: "changed"})
    assert runner._resume_cases(destination, changed, {("A", 0, "x")}) == {}


def test_load_queries_has_filename_order_independent(runner, tmp_path):
    first = tmp_path / "z.json"
    second = tmp_path / "a.json"
    first.write_text(json.dumps([{"query": "z"}]))
    second.write_text(json.dumps([{"query": "a"}]))
    assert [item[2] for item in runner._load_queries([first, second])] == ["a", "z"]


def test_call_applies_and_restores_environment(runner, tmp_path, monkeypatch):
    observed = {}

    def search(**kwargs):
        observed["reranker"] = runner.os.environ.get("SALTMDB_RERANKER_MODEL")
        observed["semantic"] = runner.os.environ.get("SALTMDB_ENABLE_SEMANTIC")
        return {"results": []}

    monkeypatch.setenv("SALTMDB_RERANKER_MODEL", "previous")
    monkeypatch.delenv("SALTMDB_ENABLE_SEMANTIC", raising=False)
    result = runner._call(
        search,
        {},
        {"SALTMDB_RERANKER_MODEL": "configured", "SALTMDB_ENABLE_SEMANTIC": "false"},
        tmp_path / "db",
    )
    assert result["output"] == {"results": []}
    assert observed == {"reranker": "configured", "semantic": "false"}
    assert runner.os.environ["SALTMDB_RERANKER_MODEL"] == "previous"
    assert "SALTMDB_ENABLE_SEMANTIC" not in runner.os.environ


def test_run_copies_input_and_requires_liveness(runner, tmp_path, monkeypatch):
    import argparse
    import types

    source = tmp_path / "input.db"
    source.write_bytes(b"snapshot")
    output_dir = tmp_path / "out"
    args = argparse.Namespace(
        db_path=str(source),
        output_dir=str(output_dir),
        query_file=[],
        expect_src="src",
        run_name="smoke",
        max_queries=None,
        sample_only=False,
    )
    fake_config = types.ModuleType("saltmdb.config")
    setattr(fake_config, "get_db_path", lambda: str(tmp_path / "live.db"))
    fake_memory = types.ModuleType("saltmdb.domain.services.memory_service")
    setattr(fake_memory, "search_memory", lambda **kwargs: {})
    fake_reranker = types.ModuleType("saltmdb.domain.services.reranker_service")
    setattr(fake_reranker, "score_pairs_preflight", lambda **kwargs: {})
    monkeypatch.setitem(sys.modules, "saltmdb.config", fake_config)
    monkeypatch.setitem(sys.modules, "saltmdb.domain.services.memory_service", fake_memory)
    monkeypatch.setitem(sys.modules, "saltmdb.domain.services.reranker_service", fake_reranker)
    monkeypatch.setattr(runner, "check_runtime", lambda: None)
    monkeypatch.setattr(runner, "validate_db_path", lambda path, live_path=None: source)
    monkeypatch.setattr(runner, "_validate_case_arguments", lambda *values: None)
    monkeypatch.setattr(runner, "_load_queries", lambda paths: [("fixture.json", 0, "query")])
    monkeypatch.setattr(
        runner,
        "_cases",
        lambda queries: [{"key": ["A", 0, "x"], "tier": "A", "arguments": {}, "environment": {}}],
    )
    monkeypatch.setattr(runner, "_snapshot_seed", lambda path: {"status": "empty"})
    monkeypatch.setattr(runner, "_source_header", lambda *values: {"input_sha256": "hash"})
    monkeypatch.setattr(runner, "_run_preflight", lambda preflight: {"ready": True})
    good = {
        "results": [{"relevance_preview": "x"}],
        "diagnostics": {"cross_encoder": {"executed": True}, "candidates": [{"in_semantic": True}]},
    }

    def execute(cases, completed, artifact_cases, *values, **kwargs):
        for case in cases:
            item = {**case, "output": good}
            completed[tuple(case["key"])] = item
            artifact_cases.append(item)

    monkeypatch.setattr(runner, "_execute_cases", execute)
    runner.run(args)
    assert (output_dir / "gate.db").read_bytes() == source.read_bytes()
    assert json.loads((output_dir / "smoke.json").read_text())["complete"] is True

    def execute_bad(cases, completed, artifact_cases, *values, **kwargs):
        for case in cases:
            item = {**case, "output": {"results": []}}
            completed[tuple(case["key"])] = item
            artifact_cases.append(item)

    monkeypatch.setattr(runner, "_execute_cases", execute_bad)
    args.run_name = "bad"
    with pytest.raises(ValueError, match="liveness"):
        runner.run(args)


def test_header_environment_order_is_ignored_but_values_matter(comparer, artifact):
    baseline = copy.deepcopy(artifact)
    candidate = copy.deepcopy(artifact)
    baseline["header"]["environment"] = {"A": "1", "B": "2"}
    candidate["header"]["environment"] = {"B": "2", "A": "1"}
    assert differences(comparer, baseline, candidate) == []
    candidate["header"]["environment"]["B"] = "changed"
    assert differences(comparer, baseline, candidate)


@pytest.mark.parametrize("field", ["query", "arguments", "environment"])
def test_resume_rejects_changed_case_identity(runner, tmp_path, field):
    destination = tmp_path / "partial.json"
    header = {
        "input_sha256": "one",
        "runner_sha256": "r",
        "src_head": "h",
        "src_status_src": "",
        "environment": {},
        "model_sha256": {},
        "package_versions": {},
    }
    fresh = [
        {"key": ["A", 0, "x"], "query": "q", "arguments": {"mode": "broad"}, "environment": {}}
    ]
    destination.write_text(json.dumps({"header": header, "cases": [fresh[0]]}))
    changed = dict(fresh[0])
    changed[field] = "different"
    assert runner._resume_cases(destination, header, [changed]) == {}


def test_resume_rejects_changed_source_status(runner, tmp_path):
    destination = tmp_path / "partial.json"
    header = {
        "input_sha256": "one",
        "runner_sha256": "r",
        "src_head": "h",
        "src_status_src": "",
        "environment": {},
        "model_sha256": {},
        "package_versions": {},
    }
    case = {"key": ["A", 0, "x"], "query": "q", "arguments": {}, "environment": {}}
    destination.write_text(json.dumps({"header": header, "cases": [case]}))
    changed = dict(header, src_status_src=" M src/changed.py")
    assert runner._resume_cases(destination, changed, [case]) == {}


def test_supersedes_family_cases_are_generated(runner):
    seed = {
        "title": "new title",
        "newest_titles": ["new title"],
        "tags": [],
        "alias_tags": [],
        "metadata": {},
        "memory_type": "fact",
        "is_core": False,
        "supersedes": [
            {"id": 1, "newer_title": "new title words here", "older_title": "old title"},
            {"id": 2, "newer_title": "second newer title", "older_title": "second older title"},
        ],
    }
    cases = runner._derived_cases(seed, [("fixture.json", 0, "query")])
    family = [case for case in cases if case["key"][2].startswith("supersedes_")]
    assert len(family) == 36
    assert len({tuple(case["key"]) for case in family}) == 36
    assert {case["arguments"]["collapse_supersedes_families"] for case in family} == {False, True}
    expected_added = {
        f"supersedes_{edge_index}_{query_kind}_{suffix}"
        for edge_index in range(2)
        for query_kind in ("newer", "older", "keyword")
        for suffix in ("broad_demote_durable", "strict_demote")
    }
    added = [case for case in family if case["key"][2] in expected_added]
    assert {case["key"][2] for case in added} == expected_added
    broad_added = [case for case in added if case["key"][2].endswith("_broad_demote_durable")]
    assert len(broad_added) == 6
    assert all(
        case["arguments"]["mode"] == "broad"
        and case["arguments"]["prefer_durable_types"] is True
        and case["arguments"]["demote_superseded"] is True
        for case in broad_added
    )
    strict_added = [case for case in added if case["key"][2].endswith("_strict_demote")]
    assert len(strict_added) == 6
    assert all(case["arguments"]["mode"] == "strict" for case in strict_added)


def test_snapshot_seed_reads_current_supersedes_edges(runner, tmp_path, monkeypatch):
    import types

    class Result:
        def __init__(self, rows=(), row=None):
            self.rows = rows
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.sql = []

        def execute(self, statement, parameters=()):
            self.sql.append(statement)
            if "FROM entities WHERE status" in statement:
                return Result(rows=[("id", "new", "agent", "ctx", "session", 0, "fact", "{}")])
            if "GROUP BY title" in statement:
                return Result(row=None)
            if "FROM tags" in statement:
                return Result(rows=[])
            if "FROM relations" in statement:
                return Result(rows=[(1, "new", "old")])
            raise AssertionError(statement)

        def close(self):
            return None

    connection = Connection()
    module = types.ModuleType("saltmdb.db.connection")
    setattr(module, "open_read_connection", lambda path: connection)
    monkeypatch.setitem(sys.modules, "saltmdb.db.connection", module)
    seed = runner._snapshot_seed(tmp_path / "copy.db")
    assert seed["supersedes"] == [{"id": 1, "newer_title": "new", "older_title": "old"}]
    relation_sql = next(statement for statement in connection.sql if "FROM relations" in statement)
    assert "valid_from" in relation_sql and "valid_to" in relation_sql
    assert "datetime(r.valid_from)" in relation_sql
    assert "ORDER BY r.id LIMIT 3" in relation_sql
