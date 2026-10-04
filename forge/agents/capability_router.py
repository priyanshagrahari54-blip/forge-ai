"""Capability-first agent routing facade."""
def route(candidates,capabilities):
    needed=set(capabilities or ())
    ranked=[]
    for c in candidates or ():
        caps=set(getattr(c,"capabilities",()) or ())
        ranked.append((len(needed & caps),c))
    return [c for _,c in sorted(ranked,key=lambda x:x[0],reverse=True)]
