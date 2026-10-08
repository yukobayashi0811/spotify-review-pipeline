from e1 import *
import random,sys
def avg(sc): return round(float(np.mean([a for a,_ in sc])),4)
for seed in (1,2):
    random.seed(seed); pool=train[:]; random.shuffle(pool)
    base=pool[:5000]; rest=pool[5000:]
    v,m=fit(base); b=avg(score(joint(probs(v,m,test),500),test))
    P=probs(v,m,rest); mm=np.mean([np.sort(P[t][1],1)[:,-1]-np.sort(P[t][1],1)[:,-2] for t in T],0)
    sel=[rest[i] for i in np.argsort(mm)[:1500]]
    v2,m2=fit(base+sel); u=avg(score(joint(probs(v2,m2,test),500),test))
    v3,m3=fit(base+rest[:1500]); r=avg(score(joint(probs(v3,m3,test),500),test))
    print(seed,'base5k',b,'+1.5k random',r,'+1.5k uncertain',u,flush=True)
