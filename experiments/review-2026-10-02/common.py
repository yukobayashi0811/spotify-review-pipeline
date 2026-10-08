import csv,unicodedata,glob,os
from pathlib import Path
R=str(Path(__file__).resolve().parents[2])
def rd(p): return list(csv.DictReader(open(p,encoding='utf-8-sig',newline='')))
def norm(t): return ' '.join(unicodedata.normalize('NFKC',t).casefold().split())
src=rd(R+'/data/analysis_10000.csv')
lab={r['review_id']:r for r in rd(R+'/experiments/codex_luna/checkpoint-10000-normal-usage-v2/predictions.csv')}
gold=rd(R+'/evals/golden_50.csv')
test500=src[:500]
excl=set(norm(r['review_text']) for r in test500+gold)
groups={}
for r in src[500:]:
    n=norm(r['review_text'])
    if n in excl: continue
    l=lab[r['review_id']]
    groups.setdefault(n,[]).append((r,l))
train=[]
for n,g in sorted(groups.items()):
    tup={(l['topic'],l['intent'],l['severity']) for _,l in g}
    if len(tup)!=1: continue
    r,l=min(g,key=lambda x:x[0]['review_id'])
    train.append(dict(text=r['review_text'],topic=l['topic'],intent=l['intent'],severity=l['severity'],sentiment=float(l['sentiment'])))
test=[dict(text=r['review_text'],topic=lab[r['review_id']]['topic'],intent=lab[r['review_id']]['intent'],severity=lab[r['review_id']]['severity']) for r in test500]
goldt=[dict(text=r['review_text'],topic=r['topic'],intent=r['intent'],severity=r['severity']) for r in gold]
