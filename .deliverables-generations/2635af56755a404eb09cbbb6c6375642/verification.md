# Developer verification and operating limits

Human requirement validation remains separate in `planning/validation.md`; no human verdict is generated here. Capture linkage, remote default branch, push and Classroom submission require the person's evidence.

## Intended assertions and reproduction

Run the README's offline setup, then `env -u API_KEY python3 -m unittest discover -s tests -v`. T01–T33 check captured source, extraction validation, ordered routing, persistence, overlapping workers, interruption recovery, coherent exports, independent accuracy counting and the native provider adapter through mocked HTTP. T34 checks a clean offline installation and documented commands. T35 checks the 400/2,000-document synthetic workloads.

```bash
python3 -m access_intake --provider mock intake materials/requests tests/inputs/IT-021.json
python3 -m access_intake.workload --output verification/workload-observations.json
python3 -m access_intake.handover --database .access-intake.sqlite --output deliverables --workloads verification/workload-observations.json
```

The intended comparisons preserve original-byte hashes and filenames, all source-derived scalar facts, exact evidence, separate processing/review states, retained earlier attempts, and one current row per document. Security Review examines risk and forwards its recommendation to the system owner; approval is a human decision and managers are contacts.

## Observed evidence

Full-suite record: `not_recorded_yet`; command `env -u API_KEY python3 -m unittest discover -s tests -v`; evidence `planning/test-run.txt`. If this generation says not_recorded_yet, it predates the saved final suite run and makes no passing claim. The final selected generation should be rebuilt after that run.

`accuracy-report.json` records this generation's observed mock fixture comparison. `request-evidence.json` is an offline request preview of the implemented adapter, with redacted authorization; it is not a live inference. T31–T33 verify its transport construction and response handling with saved native replies.

| Documents | Observed elapsed seconds | Peak provider calls in flight | Initial success/error | Final success/error | Provider calls |
| --- | --- | --- | --- | --- | --- |
| 400 | 2.329 | 2 | 399/1 | 400/0 | 401 |
| 2000 | 13.362 | 2 | 1999/1 | 2000/0 | 2001 |

Configuration and source/template fingerprints are in `workload-observations.json`. Its timings include source capture, processing, recovery, validation and export on the recorded runtime. A held first response left other results inspectable; the second document failed once and an explicit retry recovered it without replacing unaffected successes. These are observed mock runs, not live throughput promises. Test timeouts are hang guards, not office deadlines.

## Limits and review

The supported deployment is single-host SQLite on a local POSIX filesystem. Multi-host/network-filesystem operation is unverified. Each process has its own bounded pool and pacing. A timed-out underlying call keeps its slot until return; all occupied slots can leave recorded pending work for a later run. Leases must exceed request timeout plus commit margin. A crash after provider return can repeat the external call; fencing protects current results, not exactly-once external execution. Export generations and attempt history are retained without pruning. Readers must pin one generation before opening the file pair.

General override recognition is not established by the supplied attack. The implementation recognizes the reviewed pattern and has a detector seam. Broader office ambiguities remain recorded in the plan. Mock agreement does not measure live extraction accuracy. Provider availability and account access have not been verified with a real credential.

Current artifact-generation interpreter: Python 3.12.3 on Linux. Additional actually tested runtimes, when recorded, are in repository `verification/runtime-observations.json`; do not infer unrecorded version coverage.

H1 is a developer read against artifact requirements, not the person's validation verdict. Recorded H1 status: `not_recorded_yet`. Its findings/repairs are in `h1-review.json`. Host setup repair: use a prepared wheel cache and offline pip bootstrap because ensurepip is absent on the observed host.

## Producing source hashes

```json
{
  "access_intake/__init__.py": "8a57c08fd13435f5befa1a9f2e5cf4f53cdf607c5587505ea7dc8b63b893fc1e",
  "access_intake/__main__.py": "59e3e34bb27eaa51c887b0e204c90ed2715799216d56581277049d75a4a5d15e",
  "access_intake/accuracy.py": "8273113b8ab8c50ee18ae218450f5aef5ed97d42b34b228e3131b5009604be40",
  "access_intake/capture.py": "7b6eaa1a2d4fee4878b0da041093c504e21e81b35c894f83a115d48160b23f82",
  "access_intake/cli.py": "51fcd931360ebc94b00aa9e7119bfa52e62355f1c8843c4017229e836ebaf03d",
  "access_intake/exports.py": "95b0df11b9d116e6f68e6afe5b7bfbf43ee91888d01c7aa95336fd13e6cef3c8",
  "access_intake/extraction.py": "84d25795a10073de75fa5fccb264081d2f2746327ff6f48cc6a54081197d8760",
  "access_intake/handover.py": "aa033b3de0aabb73080645ca0b44e07a79e42fe9808345e8071475abf6fb28a3",
  "access_intake/openai_provider.py": "8a458a4d3ed299bb7a4cab07a25bf1c3852a12fa9f652ee405276d918a3db95d",
  "access_intake/presentation.py": "1a739ea4d47ff7603eb03febe091c7203c8c9c68f75a40ca84783fc8ac5fb977",
  "access_intake/providers.py": "984ad1c073194a7286b76582e70683548d23d2ce4cd55180277c549139d00e48",
  "access_intake/routing.py": "2e0cfb5da1ced92d28587586de76cc5a1526702bfb5480ee5b572ecadeafe44f",
  "access_intake/store.py": "66c64b0cc2dd33b399badce989e9d247ad0fd49c48735b1b8a8479c9c4007b98",
  "access_intake/workers.py": "7a278498d4a59d4fdfb2c9ed09ba9bf2dc618137b68c319c5afdae5cd37a788e",
  "access_intake/workload.py": "6b4cd5602af9905e3a55b560e3fa83884f135f3205db88d0198e84103c0ae805",
  "config/extraction-prompt.txt": "c0ddb1fe3761dd124b0e25697b168bf73af2d9b8ae641ca245059543d19dd67b",
  "config/provider.json": "f4c37671fb0720934366b5ee89bc635f3a1f8e5736e46335fd44e691268a9d2c",
  "snapshot.schema.json": "2802bed75be35046552b6d23f45447b416b440fc8ba5a62a9d1d50d0eebbc636",
  "requirements.lock": "4e313012a139180478a009ecc9157be4f985795ce62b276454fb32e18cb1c164",
  "requirements-bootstrap.lock": "1bce5f13d6d1866f194332655d0ac48a6e50898491f1a0d16b9d6c1a008bfca7"
}
```
