from e1 import *
import csv,collections,time,pickle,os
t0=time.time()
v,m=fit(train); print('fit',round(time.time()-t0,1),flush=True)
cnt=collections.Counter()
with open(R+'/data/spotify_reviews_18months.csv',encoding='utf-8-sig',newline='') as f:
    for r in csv.DictReader(f):
        if r['review_text'].strip(): cnt[r['review_text']]+=1
texts=list(cnt); sizes=np.array([cnt[t] for t in texts]); words=np.array([len(t.split()) for t in texts])
print('distinct',len(texts),flush=True)
out={k:[] for k in ('topic','intent','sev','mm','pcomp')}
t1=time.time()
for s in range(0,len(texts),20000):
    X=v.transform(texts[s:s+20000]); P={t:(m[t].classes_,m[t].predict_proba(X)) for t in T}
    mm=np.mean([np.diff(np.sort(P[t][1],1)[:,-2:],axis=1)[:,0] for t in T],0)
    ic=list(P['intent'][0]); pcomp=P['intent'][1][:,ic.index('complaint')]+P['intent'][1][:,ic.index('cancellation')]
    for t,k in (('topic','topic'),('intent','intent'),('severity','sev')):
        out[k]+= [P[t][0][i] for i in P[t][1].argmax(1)]
    out['mm'].append(mm); out['pcomp'].append(pcomp)
print('score all distinct secs',round(time.time()-t1,1),flush=True)
mm=np.concatenate(out['mm']); pc=np.concatenate(out['pcomp'])
np.savez('full_scores.npz',mm=mm,pc=pc,sizes=sizes,words=words,topic=np.array(out['topic']),intent=np.array(out['intent']),sev=np.array(out['sev']))
print('done',flush=True)
