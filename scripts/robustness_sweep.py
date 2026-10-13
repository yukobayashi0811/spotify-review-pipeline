#!/usr/bin/env python3
"""Robustness sweep: run the whole pipeline on many small random inputs (no model calls).

Samples 3, 8, 15, 30, 60, 100 and 250 reviews (4 seeds each) from data/analysis_10000.csv and runs
`run_pipeline` with config/pilot_100.json twice per input (cold, then warm). GPT-6 Luna is replaced
by an adversarial fixture: the grouping role marks clusters coherent/actionable at random (so some
inputs have no coherent and actionable issue, as happened in one 100-review pilot), and the memo role
proposes the top-ranked issue with unfiltered alternatives. Embeddings are a hash fixture. A run
passes when both invocations complete and the warm one skips every stage.

Usage: python3 scripts/robustness_sweep.py      (needs the [dev,distilled] extras; about 1 minute)
"""
import csv, random, sys, tempfile, traceback, json
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src')); sys.path.insert(0, str(ROOT / 'tests'))
import test_pipeline_run as T
from spotify_pipeline import group as group_module
from spotify_pipeline.pipeline_run import run_pipeline

class Adversarial(T.FakeLuna):
    def __init__(self, seed):
        super().__init__(); self.rng = random.Random(seed)
    def call(self, **kw):
        role = kw['role']; up = kw['user_payload']
        if role == 'group':
            res = super().call(**kw)
            for issue in res.payload['issues']:
                issue['coherent'] = 'yes' if self.rng.random() < 0.35 else 'no'
                issue['actionable'] = 'yes' if (self.rng.random() < 0.6 and not issue['issue_id'].startswith('I_OTHER')) else 'no'
            return res
        if role == 'memo':
            ranking = up['ranking']; issues = up['issues']
            elig = (up.get('priority_eligibility') or {}).get('eligible_priority_issue_ids')
            allowed = {"access", "usability", "playback", "billing", "support"}
            if not elig:
                elig = [r['issue_id'] for r in up['actionable_ranking'] if r['topic'] in allowed] or [r['issue_id'] for r in ranking if issues[r['issue_id']]['topic'] in allowed]
            top = elig[0]
            others = [r['issue_id'] for r in ranking if r['issue_id'] != top][:3]
            cov = up['coverage']
            payload = {"priority_topic": issues[top]['topic'], "priority_issue_id": top, "priority": f"Prioritize {top}.",
                       "rationale": "x", "alternative_issue_ids": others, "alternatives": [f"{o} lower" for o in others],
                       "baseline_top_handling": "x", "sensitivity_interpretation": "x",
                       "limitations": ["a", "b", "c", f"{cov['quarantined_rows']} rows quarantined"], "claims": []}
            return T.ProviderResult(request_id=f"fake-{self.rng.random()}", payload=payload, input_tokens=1, cached_input_tokens=0,
                                    output_tokens=1, duration_seconds=0.0, provider=self.name, billing_mode='synthetic',
                                    usage_status='measured', provider_metadata={})
        return super().call(**kw)

rows = list(csv.DictReader(open(ROOT / 'data/analysis_10000.csv', encoding='utf-8', newline='')))
fields = list(rows[0])
results = []
for size in (3, 8, 15, 30, 60, 100, 250):
    for seed in range(4):
        rng = random.Random(size * 100 + seed)
        sample = rng.sample(rows, size)
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / 'in.csv'
            with inp.open('w', encoding='utf-8', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(sample)
            fake = Adversarial(seed)
            try:
                with mock.patch.object(group_module, '_load_or_create_embeddings', T.fake_embeddings):
                    r = run_pipeline(inp, ROOT / 'config/pilot_100.json', output_dir=Path(tmp) / 'out', luna_provider_factory=lambda _r: fake)
                    r2 = run_pipeline(inp, ROOT / 'config/pilot_100.json', output_dir=Path(tmp) / 'out', luna_provider_factory=lambda _r: fake)
                s = json.loads((Path(tmp)/'out'/'run_summary.json').read_text())
                results.append((size, seed, 'ok', s['all_stages_skipped_unchanged']))
            except Exception as e:
                results.append((size, seed, 'FAIL', f"{type(e).__name__}: {str(e)[:160]}"))
for r in results: print(r)
failed = [r for r in results if r[2] != 'ok']
receipt = {'runs': len(results), 'failed': len(failed), 'model_calls': 0, 'config': 'config/pilot_100.json',
           'results': [{'reviews': r[0], 'seed': r[1], 'status': r[2], 'warm_skipped_all_or_error': r[3]} for r in results]}
(ROOT / 'evals/system_tests').mkdir(parents=True, exist_ok=True)
(ROOT / 'evals/system_tests/robustness_sweep.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
print(json.dumps({k: receipt[k] for k in ('runs', 'failed', 'model_calls')}))
raise SystemExit(1 if failed else 0)
