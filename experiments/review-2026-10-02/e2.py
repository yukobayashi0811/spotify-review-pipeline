from e1 import *
import random
import sys; random.seed(int(sys.argv[1])); pool=train[:]; random.shuffle(pool)
base=pool[:2000]; rest=pool[2000:]
def avg(sc): return round(float(np.mean([a for a,_ in sc])),4), round(float(np.mean([f for _,f in sc])),4)
for add in (2000,):
    # random
    v,m=fit(base+rest[:add]); print('random +',add,avg(score(joint(probs(v,m,test),500),test)),flush=True)
    # uncertainty (mean margin) from model trained on base
    v,m=fit(base); P=probs(v,m,rest)
    mm=np.mean([np.sort(P[t][1],1)[:,-1]-np.sort(P[t][1],1)[:,-2] for t in T],0)
    idx=np.argsort(mm)[:add]; sel=[rest[i] for i in idx]
    v2,m2=fit(base+sel); print('uncert +',add,avg(score(joint(probs(v2,m2,test),500),test)),flush=True)
    # hybrid: half uncertainty, half random among remaining



