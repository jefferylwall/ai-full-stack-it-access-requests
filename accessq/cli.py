"""Command-line interface: python -m accessq <command> ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import accuracy, policy
from .config import REPO_ROOT, api_key, load_settings, state_dir
from .intake import DEFAULT_INPUT, DEFAULT_OUTPUT, Intake, discover, load_source, render_outputs
from .provider import ExtractionError, MockProvider, handle_completion, make_provider
from .store import Store, atomic_write_text, utcnow


def _settings(args):
    s = load_settings(getattr(args, "config", None))
    for flag, key in (("provider", "provider"), ("model", "model"), ("base_url", "base_url"),
                      ("concurrency", "concurrency")):
        v = getattr(args, flag, None)
        if v is not None:
            setattr(s, key, v)
    return s


def _store(args) -> Store:
    st = Store(Path(args.state_dir) if args.state_dir else state_dir())
    key = api_key()
    if key:
        st.secrets.append(key)
    return st


def _intake(args, kind: str) -> Intake:
    s = _settings(args)
    st = _store(args)
    out = None if args.no_output else Path(args.output or DEFAULT_OUTPUT)
    return Intake(s, st, make_provider(s, api_key()), out, run_kind=kind)


def _print_run(run: dict, intake: Intake) -> int:
    print(f"run {run['run_id']} ({run['kind']}) {run['status']}: {run.get('counts')}")
    print(f"response_source={run['response_source']} fingerprint={run['extraction_fingerprint']} "
          f"policy={run['policy_version']}")
    errors = [d for d, o in run["outcomes"].items() if o == "error"]
    busy = [d for d, o in run["outcomes"].items() if o == "busy"]
    for d in errors:
        rec = intake.store.read_current(d) or {}
        print(f"  ERROR {d}: {rec.get('error')}")
    if busy:
        print(f"  {len(busy)} document(s) were locked by another operator and skipped: {', '.join(busy)}")
        print("  They will be completed by that operator; run `python -m accessq resume` later to pick up any left over.")
    if intake.output_dir:
        print(f"outputs: {intake.output_dir / 'queue.json'} and {intake.output_dir / 'extractions.jsonl'}")
    return 1 if errors else 0


def cmd_intake(args) -> int:
    inputs = args.inputs or [str(DEFAULT_INPUT)]
    intake = _intake(args, "intake")
    sources = discover(inputs)
    try:
        run = intake.run(sources, force=args.force)
    except KeyboardInterrupt:
        print("\nInterrupted. Completed results are saved; resume with: python -m accessq resume", file=sys.stderr)
        return 130
    return _print_run(run, intake)


def _sources_for(intake: Intake, doc_ids: list[str]) -> list:
    out = []
    for d in doc_ids:
        rec = intake.store.read_current(d)
        if not rec:
            raise SystemExit(f"unknown document {d!r}; run `python -m accessq queue` to list documents")
        p = Path(rec["source_path"])
        p = p if p.is_absolute() else REPO_ROOT / p
        if not p.exists():
            raise SystemExit(f"source file for {d} not found at {p}; rerun intake with its export")
        out.append(load_source(p))
    return out


def cmd_resume(args) -> int:
    intake = _intake(args, "resume")
    todo = [r["document_id"] for r in intake.store.all_current() if r["status"] in ("pending", "error")]
    if not todo:
        print("nothing to resume: no pending or error documents")
        return 0
    print(f"resuming {len(todo)} document(s): {', '.join(todo)}")
    try:
        run = intake.run(_sources_for(intake, todo))
    except KeyboardInterrupt:
        print("\nInterrupted again; rerun: python -m accessq resume", file=sys.stderr)
        return 130
    return _print_run(run, intake)


def cmd_retry(args) -> int:
    intake = _intake(args, "retry")
    docs = list(args.documents)
    if args.all_errors:
        docs += [r["document_id"] for r in intake.store.all_current() if r["status"] == "error"]
    if not docs:
        print("no documents to retry")
        return 0
    run = intake.run(_sources_for(intake, sorted(set(docs))), force=True)
    return _print_run(run, intake)


def cmd_queue(args) -> int:
    recs = _store(args).all_current()
    if args.status:
        recs = [r for r in recs if r["status"] == args.status]
    if args.owner:
        recs = [r for r in recs if r["owner"].lower() == args.owner.lower()]
    if args.json:
        print(json.dumps([{k: r.get(k) for k in ("document_id", "request_id", "status", "owner", "next_action",
                                                  "review_flags", "error", "source_sha256", "processed_at")}
                          for r in recs], indent=2))
        return 0
    print(f"{'DOCUMENT':<13}{'REQUEST':<9}{'STATUS':<19}{'OWNER':<17}{'NEXT ACTION':<24}FLAGS / ERROR")
    for r in recs:
        note = r.get("error") or "; ".join(r.get("review_flags") or [])
        print(f"{r['document_id']:<13}{(r.get('request_id') or '-'):<9}{r['status']:<19}{r['owner']:<17}"
              f"{r['next_action']:<24}{note[:110]}")
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\n{len(recs)} document(s): {counts}")
    return 0


def cmd_show(args) -> int:
    st = _store(args)
    rec = st.read_current(args.document)
    if not rec:
        print(f"unknown document {args.document!r}")
        return 2
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


def cmd_history(args) -> int:
    st = _store(args)
    atts = st.attempts(args.document)
    if not atts:
        print(f"no attempts recorded for {args.document!r}")
        return 2
    for a in atts:
        print(f"{a['processed_at']}  {a['attempt_id']}  {a.get('attempt_kind','extract'):<8} "
              f"sha={a['source_sha256'][:12]}  {a['status']:<18} {a['owner']:<16} "
              f"src={a.get('response_source')} fp={a.get('extraction_fingerprint')} "
              f"{('ERR ' + a['error'][:80]) if a.get('error') else ''}")
    return 0


def cmd_runs(args) -> int:
    for r in _store(args).runs():
        print(f"{r['started_at']}  {r['run_id']}  {r['kind']:<7} {r['status']:<11} {r.get('operator')}  {r.get('counts')}")
    return 0


def cmd_export(args) -> int:
    q = render_outputs(_store(args), Path(args.output or DEFAULT_OUTPUT))
    print(f"wrote queue.json and extractions.jsonl: {q['summary']}")
    return 0


def _example_bodies(message_extraction: dict) -> list[tuple[str, dict]]:
    ok = {"id": "example-ok", "model": "<model>", "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "refusal": None, "content": json.dumps(message_extraction)}}]}
    return [
        ("completed response", ok),
        ("refusal", {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": None,
                                                                       "refusal": "I can't help with that."}}]}),
        ("interrupted (max_tokens)", {"choices": [{"finish_reason": "length", "message": {
            "role": "assistant", "content": json.dumps(message_extraction)[:60]}}]}),
        ("malformed JSON content", {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "{\"request_id\": \"IT-001\","}}]}),
        ("schema violation", {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({**message_extraction, "permission": "superuser"})}}]}),
        ("quote not in source", {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({**message_extraction,
                                                         "supporting_quotes": ["I need admin on Atlas"]})}}]}),
        ("no choices", {"id": "x", "choices": []}),
    ]


def cmd_evidence(args) -> int:
    s = _settings(args)
    src = load_source(Path(args.document) if Path(args.document).exists() else DEFAULT_INPUT / args.document)
    live = make_provider(s, api_key())
    req = live.redacted_request(src.document_id, src.message)
    mock_ext = MockProvider(s).extract(src.document_id, src.message).extraction
    handled = []
    for label, body in _example_bodies(mock_ext):
        try:
            ext, meta = handle_completion(body, src.message)
            handled.append({"case": label, "result": "accepted", "finish_reason": meta["finish_reason"]})
        except ExtractionError as e:
            handled.append({"case": label, "result": "rejected", "error": e.as_dict(),
                            "queue_effect": "status=error owner=IT Service Desk next_action=retry-extraction"})
    evidence = {
        "generated_at": utcnow(),
        "note": "Credential-redacted. The Authorization header value is replaced; API_KEY is read only from the "
                "environment. Request built by accessq/provider.py OpenAICompatibleProvider.build_request.",
        "what_this_is": "The exact request accessq sends for this document under the configured settings, built "
                        "without sending it. No provider call is made by this command; handled_examples run "
                        "synthetic response bodies through the real response handler.",
        "document_id": src.document_id,
        "source_sha256": src.sha256,
        "provider": s.provider,
        "model_id": s.model,
        "response_source_label": s.response_source(),
        "extraction_settings": s.extraction_settings(),
        "extraction_fingerprint": s.extraction_fingerprint(),
        "operational_settings": {k: getattr(s, k) for k in ("timeout_seconds", "max_retries", "backoff_base_seconds",
                                                             "backoff_max_seconds", "concurrency",
                                                             "lock_lease_seconds")},
        "policy_version": policy.policy_version(),
        "request": req,
        "response_handling": {
            "transport": [
                "HTTP 429 -> rate_limited, retried with exponential backoff + jitter (Retry-After honoured, capped "
                "at backoff_max_seconds) up to max_retries",
                "HTTP 5xx, timeout, network error -> server_error/timeout/network, retried the same way",
                "HTTP 401/403 -> auth (not retried; check API_KEY)",
                "other non-200 -> bad_request (not retried)",
                "non-JSON body -> malformed_response",
            ],
            "completion": [
                "body.error present -> provider_error",
                "no choices -> malformed_response",
                "message.refusal non-empty -> refusal",
                "finish_reason=content_filter -> content_filter",
                "finish_reason=length -> interrupted (truncated)",
                "finish_reason not 'stop' (incl. null) -> interrupted",
                "empty content -> malformed_response",
                "content not JSON -> invalid_output",
                "JSON fails snapshot.schema.json, non YYYY-MM-DD date, or a supporting quote that is not an exact "
                "substring of the message -> invalid_output",
                "only then: extraction accepted and routed by accessq/policy.py",
            ],
            "on_failure": "record status=error, owner=IT Service Desk, next_action=retry-extraction, "
                          "extraction=null, error=<code: message. Retry with: python -m accessq retry <document_id>>; "
                          "the previous valid attempt stays in state/attempts and is referenced by last_valid_attempt",
        },
        "handled_examples": handled,
    }
    out = Path(args.out or (DEFAULT_OUTPUT / "request-evidence.json"))
    text = json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    key = api_key()
    if key and key in text:  # defence in depth
        raise SystemExit("refusing to write evidence: API key found in output")
    atomic_write_text(out, text)
    print(f"wrote {out}")
    return 0


def cmd_accuracy(args) -> int:
    s = _settings(args)
    exp = accuracy.load_expected(Path(args.expected) if args.expected else accuracy.EXPECTED_PATH)
    recs = {}
    path = Path(args.extractions or (DEFAULT_OUTPUT / "extractions.jsonl"))
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            recs[r["document_id"]] = r
    sources = {r["response_source"] for r in recs.values()}
    rep = accuracy.compare(recs, exp["supplied"])
    out = [accuracy.format_report(f"Supplied messages ({path.name}; response_source={sorted(map(str, sources))})", rep)]
    # Synthetic paraphrase(s): extracted now with the configured provider, routed, compared.
    syn_recs = {}
    for src in discover([REPO_ROOT / "tests" / "fixtures" / "synthetic"]):
        prov = make_provider(s, api_key())
        try:
            res = prov.extract(src.document_id, src.message)
            r = policy.route(src.document_id, src.message, res.extraction)
            syn_recs[src.document_id] = {"extraction": res.extraction, **r, "response_source": res.response_source}
        except ExtractionError as e:
            syn_recs[src.document_id] = {"extraction": None, **policy.error_route(), "error": e.message,
                                         "response_source": s.response_source()}
    syn = accuracy.compare(syn_recs, exp["synthetic"])
    out.append(accuracy.format_report(f"Synthetic paraphrase (response_source={s.response_source()})", syn))
    text = "\n".join(out)
    print(text)
    if args.json:
        Path(args.json).write_text(json.dumps({"supplied": rep, "synthetic": syn}, indent=2) + "\n")
    if args.min_rate is not None and (rep["exact_match_rate"] or 0) < args.min_rate:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m accessq", description="IT access-request intake and review queue")
    p.add_argument("--state-dir", help="state directory (default ./state or $ACCESSQ_STATE_DIR)")
    p.add_argument("--config", help="settings file (default config/model.json or $ACCESSQ_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    def provider_flags(sp):
        sp.add_argument("--provider", choices=["openai-compatible", "mock"])
        sp.add_argument("--model")
        sp.add_argument("--base-url", dest="base_url")
        sp.add_argument("--concurrency", type=int)
        sp.add_argument("--output", help="deliverables directory (default ./deliverables)")
        sp.add_argument("--no-output", action="store_true", help="do not render queue.json/extractions.jsonl")

    sp = sub.add_parser("intake", help="process export files or directories (default materials/requests)")
    sp.add_argument("inputs", nargs="*")
    sp.add_argument("--force", action="store_true", help="re-extract even when source and settings are unchanged")
    provider_flags(sp)
    sp.set_defaults(func=cmd_intake)

    sp = sub.add_parser("resume", help="process every pending or error document from its recorded source")
    provider_flags(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("retry", help="re-extract specific documents")
    sp.add_argument("documents", nargs="*")
    sp.add_argument("--all-errors", action="store_true")
    provider_flags(sp)
    sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("queue", help="show the live review queue")
    sp.add_argument("--status", choices=["review", "needs-information", "error", "pending"])
    sp.add_argument("--owner")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("show", help="show the current record for a document")
    sp.add_argument("document")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("history", help="list every recorded attempt for a document")
    sp.add_argument("document")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("runs", help="list intake/resume/retry runs")
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("export", help="re-render deliverables from current state")
    sp.add_argument("--output")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("evidence", help="write a credential-redacted example request")
    sp.add_argument("--document", default="IT-001.json")
    sp.add_argument("--out")
    provider_flags(sp)
    sp.set_defaults(func=cmd_evidence)

    sp = sub.add_parser("accuracy", help="compare extractions with source-derived expected fields")
    sp.add_argument("--extractions")
    sp.add_argument("--expected")
    sp.add_argument("--json", help="also write the full report as JSON to this path")
    sp.add_argument("--min-rate", type=float, help="exit 1 if the supplied exact-match rate is below this")
    provider_flags(sp)
    sp.set_defaults(func=cmd_accuracy)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
