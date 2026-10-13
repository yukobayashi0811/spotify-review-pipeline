# Code provenance of the final run

`run_manifest.json` records `code_commit` = `5daeee3…`, the last commit **before** the final run. The run itself used the working tree on top of that commit, which had not been committed yet; it is committed together with these results. To make that checkable, the SHA-256 of every package source file as it was when the run executed is listed below (prompt and config hashes are in `run_manifest.json`). From now on `run_manifest.json` also records `code_uncommitted_changes` and these per-file hashes automatically.

| File (`src/spotify_pipeline/`) | SHA-256 at run time | Now |
|---|---|---|
| `__init__.py` | `8ea08ec2acf6dfdd7071d00aae151ec85b2bc916eceb4fdefdc62b62ba58d86f` | same |
| `classify.py` | `75460227d7b13234ee83819ac39353a125920b3af5d2299746715e1c726354eb` | same |
| `cli.py` | `b8c98bc164275e544e5c4014f4b792e50098252a8e33cccfd33041e001b2a340` | same |
| `codex_benchmark.py` | `c8fa347066a3ab8bd97c66490c6380a5bb3c6dbee71173e92f46347b0ac5d2a6` | same |
| `constants.py` | `e51871e0cc428fc3ca04f838cfcb608f22f8dc9c8be35bff6c7cbfc3b4107248` | same |
| `costs.py` | `8a6262bfe28fc383a27443c72a1753af8be3e6b4548f59c021c004eb4ac9baca` | same |
| `distilled.py` | `aab9a510a90ea9b3751f03edbd1da84187f4832fc9cddc65e58ec20ddb0709bb` | same |
| `downstream_analysis.py` | `4a86fdd6a37203922a149d0634923f892e9cfa3b1aac58bcf15441e3be3c4408` | same |
| `evaluate.py` | `4024ecd8412af99d6e6cc1cf78f2a17b36036175555605d3ea3a4ab9f289c1c7` | changed after the run (see below) |
| `export.py` | `f91bb596c9a214d94761df5f761c12498e0c7cca6184e51087abd3d95b61b0e9` | same |
| `frozen_run.py` | `1950e8ad95b6e9ce1e5e8fbb3a4091921a20e0db0094f7e2e433706e5771fdd7` | same |
| `full_run.py` | `aa2479f5d73ff8a6c909fa672345a4759d4f322b71ca78b0a4c161de32799d7a` | same |
| `group.py` | `9dc77d5adb7f3aeadb0860010aed0219418375c5062eae7b8741d3c533afc692` | same |
| `guard.py` | `fd8b1d616b9f01b54177ccd900cbcd6082581c013b91dd1b95349315bfb6ff6f` | same |
| `hybrid.py` | `10c9165f46367af1983e42538678043c4171a06ce3067bf448d0ec771df9a87e` | same |
| `io_utils.py` | `12b01765c524ce14c2388ce6138e3295c318b807b228927382c29265ac382fde` | same |
| `legacy_luna.py` | `f8897bcf4634a49a40b1df7a8d1640e2f195ce6d5ff4a4f2d2e243e60c7890cd` | same |
| `orchestrator.py` | `4c2258280ddaaed59a93d5c6de76a8f73aa54cbdafa7935243fe752d57be7862` | same |
| `pilot_group.py` | `1f3ae0d39cc6fd552caf631afed76e5d1e1f584f8087bed9551ee34f2dc7274f` | same |
| `pipeline_run.py` | `4dcd554b08ff7204b3f76a5cac1e2ac159a8655f6d3e006fe89bfa5aecf3f418` | changed after the run (see below) |
| `prepare.py` | `f622a130ce58eaf4b481bafbc97ca0fc2c36ba36bddaebd5406657373f231676` | same |
| `providers.py` | `d48b11ed01019830ed2b533c00f0b237d3bdd7ab02a03ea5b3175522cc42d411` | same |
| `rank.py` | `8b8197dd3c2aa841c06689df3b918bf4f97e5fa2880315e15c12f81def409078` | same |
| `recommend.py` | `08453b7cf10342d19569e4d87cfa85e18b2e8193f6bdc6ae0a3e82425877a741` | changed after the run (see below) |
| `schemas.py` | `12e2ff2c66a0b82feef4da776932a352d5e47c91480faa0a048173f56972c5dc` | same |
| `sensitivity.py` | `9e49929d9b3b35e8c8015bd9387ce255aa5ede90a6a3261cc3e63e86c93a0e16` | same |
| `storage.py` | `1211e0bd5f35e0dd7717185f177dd9b67552cb9ba3c6928ef3446e3c13b88ed8` | same |
| `verify.py` | `d85cc27d30c92aae92b876cde8b5953f115603643609228b907f589813614977` | same |

Changes made after the full run finished (none of them touches a stage the full run executed with different inputs, and none changes a saved number):

- `recommend.py`: the memo validator now requires `min(2, eligible issues)` alternatives instead of exactly two. The 100-review pilot has only one eligible alternative, so its memo failed validation; the full run has 19 eligible alternatives and passed under the old rule. Fixed before the pilot was rerun.
- `evaluate.py`: the Golden scorer's row-count check now uses its `expected_rows` argument (it still said 50), so the held-out 30 can be scored with the same code. Not used by the pipeline run.
- `pipeline_run.py`: `run_manifest.json` now also records `code_uncommitted_changes` and per-file source hashes (this file exists because the final run's manifest did not).
