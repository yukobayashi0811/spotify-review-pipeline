from common import *
import numpy as np, itertools, random, time
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
T=['topic','intent','severity']
def vec():
    return FeatureUnion([('w',TfidfVectorizer(lowercase=True,strip_accents='unicode',ngram_range=(1,2),min_df=2,max_df=.995,max_features=60000,sublinear_tf=True)),
                         ('c',TfidfVectorizer(analyzer='char_wb',ngram_range=(3,5),min_df=2,max_features=80000,sublinear_tf=True))])
def fit(tr,C=4.0,cw='balanced'):
    v=vec(); X=v.fit_transform([r['text'] for r in tr]); m={}
    for t in T:
        m[t]=LogisticRegression(C=C,class_weight=cw,max_iter=1000).fit(X,[r[t] for r in tr])
    return v,m
def probs(v,m,data):
    X=v.transform([r['text'] for r in data]); return {t:(m[t].classes_,m[t].predict_proba(X)) for t in T}
def score(pred,data):
    out=[]
    for t in T:
        y=[r[t] for r in data]; p=pred[t]
        out.append((round(np.mean([a==b for a,b in zip(y,p)]),3), round(f1_score(y,p,average='macro'),3)))
    return out
def argmax(P):
    return {t:[P[t][0][i] for i in P[t][1].argmax(1)] for t in T}
def ok(intent,sev):
    s=int(sev)
    if intent in('praise','request','unclear'): return s==1
    return s>=2
def joint(P,n):
    res={t:[] for t in T}
    for i in range(n):
        best=None
        for (a,pa),(b,pb),(c,pc) in itertools.product(*[list(zip(P[t][0],P[t][1][i])) for t in T]):
            if not ok(b,c): continue
            if b=='praise' and False: pass
            s=np.log(pa+1e-9)+np.log(pb+1e-9)+np.log(pc+1e-9)
            if best is None or s>best[0]: best=(s,a,b,c)
        for t,val in zip(T,best[1:]): res[t].append(val)
    return res
if __name__=="__main__":
  random.seed(0); sh=train[:]; random.shuffle(sh)
  for N in (1000,2000,4000,len(sh)):
    t0=time.time(); v,m=fit(sh[:N])
    P=probs(v,m,test); G=probs(v,m,goldt)
    a=argmax(P); j=joint(P,len(test))
    print(N,'test500 argmax',score(a,test),'joint',score(j,test),'| gold argmax',score(argmax(G),goldt),'joint',score(joint(G,50),goldt),f'{time.time()-t0:.0f}s',flush=True)
