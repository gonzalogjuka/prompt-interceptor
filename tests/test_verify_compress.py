"""Deterministic safety nets: exact values kept by the rewrite, and log compression."""
from helpers import STRUCTURE, Upstream, main_calls, make, metrics, post, structurer_calls
from interceptor.compress import compress
from interceptor.gate import MARKER_TAG
from interceptor.verify import check, extract_facts

POD = """no levanta, no ejecutes nada
Name:         api-backend-7d9f8b6c4-x2kqp
Node:         aks-nodepool1-31718369-vmss000002
    Image:          myregistry.azurecr.io/api-backend:2.4.1
      Reason:       OOMKilled
      Exit Code:    137
      memory:  256Mi
  2026-10-05T14:02:11Z  Warning  BackOff  kubelet
la 2.3.0 andaba bien"""

BIG_LOG = "el api se cae:\n" + "\n".join(
    f"2026-10-05T14:{i // 60:02d}:{i % 60:02d}Z ERROR pool exhausted acquiring conn db-prod-01:5432"
    for i in range(200)) + "\n2026-10-05T14:05:01Z FATAL OOM in worker-7f3a"


# ---- verify ------------------------------------------------------------------
def test_extract_facts_finds_identifiers_and_skips_timestamps():
    facts = extract_facts(POD)
    for f in ["api-backend-7d9f8b6c4-x2kqp", "aks-nodepool1-31718369-vmss000002",
              "myregistry.azurecr.io/api-backend:2.4.1", "OOMKilled", "137", "256Mi", "2.3.0"]:
        assert f in facts
    assert not any(f.startswith("2026-10-05") for f in facts)


def test_check_appends_missing_facts_or_rejects_a_lossy_rewrite():
    improved = "Pod OOMKilled, exit 137, limit 256Mi, 2.3.0 andaba. Imagen myregistry.azurecr.io/api-backend:2.4.1"
    fixed, rep = check(POD, improved)
    assert fixed.startswith(improved) and "api-backend-7d9f8b6c4-x2kqp" in fixed
    assert rep.added and not rep.rejected
    fixed, rep = check(POD, "Tengo un problema con un pod, ayudame.")
    assert fixed is None and rep.rejected


def test_rejected_rewrite_falls_back_to_append_mode(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": "Tengo un problema con un pod."})
    client, mpath = make(tmp_path, up)
    post(client, POD + "\n" + "x" * 400)
    sent = main_calls(up)[0]["body"]["messages"][0]["content"]
    assert sent[0]["text"].startswith(POD) and MARKER_TAG in sent[1]["text"]
    rec = metrics(mpath)[0]
    assert rec["mode"] == "append" and rec["verify_rejected"] is True


# ---- compress ----------------------------------------------------------------
def test_compress_collapses_repeats_and_keeps_unique_lines():
    out, n = compress(BIG_LOG)
    assert n == 199 and "[x200" in out and "FATAL OOM in worker-7f3a" in out
    assert len(out) < len(BIG_LOG) / 10


def test_compress_leaves_code_and_short_text_alone():
    code = "def f():\n" + "    return None\n" * 50
    assert compress(code, min_chars=10) == (code, 0)
    assert compress("hola", min_chars=3000) == ("hola", 0)


def test_compressed_text_is_what_interpreter_and_model_receive(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": "db-prod-01:5432 worker-7f3a pool exhausted"})
    client, mpath = make(tmp_path, up)
    post(client, BIG_LOG)
    interpreter_input = structurer_calls(up)[0]["body"]["messages"][0]["content"]
    assert "[x200" in interpreter_input and len(interpreter_input) < len(BIG_LOG) / 5
    assert "[x200" in main_calls(up)[0]["body"]["messages"][0]["content"][1]["text"]
    assert metrics(mpath)[0]["collapsed_lines"] == 199
