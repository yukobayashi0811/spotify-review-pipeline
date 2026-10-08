from e1 import *
v,m=fit(train); P=probs(v,m,test); j=joint(P,500)
import collections
b=collections.defaultdict(lambda:[0,0,0,0])
for i,r in enumerate(test):
    w=len(r['text'].split()); k='<=5' if w<=5 else '6-15' if w<=15 else '16-40' if w<=40 else '>40'
    e=b[k]; e[0]+=1; e[1]+=j['topic'][i]!=r['topic']; e[2]+=j['intent'][i]!=r['intent']; e[3]+=j['severity'][i]!=r['severity']
for k in ('<=5','6-15','16-40','>40'):
    n,t,i,s=b[k]; print(k,n,'err topic %.2f intent %.2f sev %.2f'%(t/n,i/n,s/n))
# complaint-only subset accuracy (teacher complaint/cancel)
idx=[i for i,r in enumerate(test) if r['intent'] in('complaint','cancellation')]
print('teacher complaints',len(idx),'topic acc %.3f sev acc %.3f'%(np.mean([j['topic'][i]==test[i]['topic'] for i in idx]),np.mean([j['severity'][i]==test[i]['severity'] for i in idx])))
pc=[i for i in range(500) if j['intent'][i] in('complaint','cancellation')]; tc=set(idx)
print('complaint-set precision %.3f recall %.3f'%(len(set(pc)&tc)/len(pc),len(set(pc)&tc)/len(tc)))
