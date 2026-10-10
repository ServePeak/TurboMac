import json, sys, statistics as st, glob, os
f = max(glob.glob('/var/lib/turbomac/samples/*.jsonl'), key=os.path.getmtime)
rows = [json.loads(l) for l in open(f) if l.startswith('{"ts')]
W = [d for d in rows if sys.argv[1] <= d['time'][11:19] <= sys.argv[2]]
print('time     pkgC  pkgW  inW   mhz(mean/min)  busy th ph  dThrCore  fans          TCXC')
prev = None
for i in range(0, len(W), 5):
    B = W[i:i+5]; d = B[-1]; c = d['cpu']
    thr = c.get('throttle_core'); dthr = (thr - prev) if prev is not None else 0; prev = thr
    mh = st.mean(st.mean(x['cpu']['mhz']) for x in B); mn = min(min(x['cpu']['mhz']) for x in B)
    print(d['time'][11:19], f"{max(x['pkg_c'] for x in B):4.0f} {st.mean(x['pkg_w'] for x in B):5.1f} {st.mean(x['input_w'] for x in B):5.1f}"
          f"  {mh:5.0f}/{mn:4d}  {st.mean(x['cpu']['busy_pct'] for x in B):5.1f} {sum(x['msr']['thermal'] for x in B)}  {sum(x['msr']['prochot'] for x in B)}"
          f"  {dthr:7d}  {[fn['rpm'] for fn in d['fans']]} {d['smc_temps'].get('TCXC')}")
for d in W:
    if d['msr']['prochot']:
        print('PROCHOT', d['time'][11:19], d['pkg_c'], d['pkg_w'], d['input_w'], d['cpu']['mhz'], d['cpu']['busy_pct'])
