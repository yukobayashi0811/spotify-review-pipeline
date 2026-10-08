from e1 import *
import re
v,m=fit(train); P=probs(v,m,test); j=joint(P,500)
split=lambda t:[s for s in re.split(r'(?<=[.!?\n])\s+|\s*[;\n]\s*',t) if len(s.split())>=3]
tc=m['topic'].classes_; sc=m['severity'].classes_
def seg_topic(t,default):
    ss=split(t)
    if len(ss)<2: return default, None
    X=v.transform(ss); pt=m['topic'].predict_proba(X); ps=m['severity'].predict_proba(X)
    es=ps@np.array([int(c) for c in sc])
    best=None
    for k,s in enumerate(ss):
        top=tc[pt[k].argmax()]
        if top=='other': continue
        key=(round(es[k],1), -k)
        if best is None or key>best[0]: best=(key,top,s)
    return (best[1],best[2]) if best else (default,None)
for lo in (16,):
    idx=[i for i,r in enumerate(test) if len(r['text'].split())>=lo]
    base=np.mean([j['topic'][i]==test[i]['topic'] for i in idx])
    seg=[seg_topic(test[i]['text'],j['topic'][i])[0] for i in idx]
    # only override when whole-text topic uncertain
    tm=np.sort(P['topic'][1],1); marg=tm[:,-1]-tm[:,-2]
    seg2=[seg[k] if marg[i]<0.3 else j['topic'][i] for k,i in enumerate(idx)]
    print('long n',len(idx),'whole-text topic acc %.3f  segment %.3f  segment-if-uncertain %.3f'%(base,np.mean([a==test[i]['topic'] for a,i in zip(seg,idx)]),np.mean([a==test[i]['topic'] for a,i in zip(seg2,idx)])))
