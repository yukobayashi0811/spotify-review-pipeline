from e1 import *
import sqlite3,glob
db=glob.glob(R+'/artifacts/runtime/checkpoint-500.db')[0]
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
G={r['review_id']:r for r in c.execute('select review_id,topic,intent,severity from records')}
ids=[r['review_id'] for r in test500]
g={t:[str(G[i][t]) for i in ids] for t in T}
v,m=fit(train); P=probs(v,m,test); j=joint(P,500)
mm=np.mean([np.sort(P[t][1],1)[:,-1]-np.sort(P[t][1],1)[:,-2] for t in T],0)
y={t:[r[t] for r in test] for t in T}
acc=lambda p,idx:[round(np.mean([p[t][i]==y[t][i] for i in idx]),3) for t in T]
allidx=list(range(500))
print('vs Luna on 500  TF-IDF',acc(j,allidx),' Gemma',acc(g,allidx))
o=np.argsort(mm)
for f in (.1,.2,.3):
    idx=list(o[:int(500*f)]); print(f'lowest-margin {int(f*100)}%: TF-IDF',acc(j,idx),'Gemma',acc(g,idx))
# cascade: lowest X% -> Gemma ; agreement gating
for f in (.2,.3,.5):
    sel=set(o[:int(500*f)].tolist())
    casc={t:[g[t][i] if i in sel else j[t][i] for i in allidx] for t in T}
    agree=[i for i in sel if all(g[t][i]==j[t][i] for t in T)]
    dis=[i for i in sel if i not in set(agree)]
    # gating: disagreements go to Luna (counted as correct vs Luna)
    gate={t:[y[t][i] if i in set(dis) else j[t][i] for i in allidx] for t in T}
    print(f'cascade {int(f*100)}% to Gemma:',acc(casc,allidx),f'| gate: agree {len(agree)} accept, {len(dis)} to Luna ->',acc(gate,allidx))
# long complaints
lc=[i for i in allidx if y['intent'][i] in('complaint','cancellation') and len(test[i]['text'].split())>=16]
print('long complaints n',len(lc),'TF-IDF',acc(j,lc),'Gemma',acc(g,lc))
