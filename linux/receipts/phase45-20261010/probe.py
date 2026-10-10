import json, statistics as st, subprocess, sys, time
sys.path.insert(0, "/opt/turbomac/src")
from turbomac.protocol import Client
cl = Client()
target = float(sys.argv[1]); names = sys.argv[2].split(","); secs = sys.argv[3] if len(sys.argv) > 3 else "12"; tguard = sys.argv[4] if len(sys.argv) > 4 else "95"
def st_():
    s = cl.call("status"); return s["governor"]["pl1_w"], s["cpu"]["temps"]["pkg_c"]
loads = {"matrixprod": ["--cpu","8","--cpu-method","matrixprod"], "fft": ["--cpu","8","--cpu-method","fft"],
         "fma": ["--fma","8"], "vecfp": ["--vecfp","8"], "matrix": ["--matrix","8"], "vecwide": ["--vecwide","8"]}
for name in names:
    args = loads[name]
    while True:
        pl1, c = st_()
        if pl1 == target and c <= 60: break
        time.sleep(2)
    out = f"probe-{name}-{target:g}w-{secs}s.json"
    r = subprocess.run(["python3","/root/turbomac-src/tools/phase3_run.py","--label",name,"--seconds",secs,"--abort-input-w","72",
        "--abort-temp-c",tguard,"--settle","3","--json",out,"--","stress-ng",*args,"--timeout",secs+"s","--metrics-brief"],
        capture_output=True, text=True)
    d = json.load(open(out)); s = d["summary"]; L = [x for x in d["rows"] if 4 <= x["t"] <= (s["ended_at_s"] or float(secs))]
    bogo = [l for l in (r.stdout+r.stderr).splitlines() if "metrc" in l and l.split()[3:4] and l.split()[3][0].isalpha() and "stressor" not in l]
    c = s["counters_delta"]
    print(f"{name:10s} pkg {st.mean(x['pkg_w'] for x in L):5.1f} W (peak {s['peak']['pkg_w']:.1f})  in {st.mean(x['input_w'] for x in L):5.1f} W (peak {s['peak']['input_w']:.1f})  "
          f"mhz {st.mean(st.mean(x['mhz']) for x in L):5.0f}  PL! {100*sum(1 for x in L if x['power_limited'])/len(L):3.0f}%  "
          f"peakC {s['peak']['pkg_c']:.0f}  pl1min {s['pl1_min']}  cuts {c.get('cuts')} em {c.get('emergencies')}  aborted {s['aborted']}  "
          f"ops/s {bogo[-1].split()[-2] if bogo else '?'}", flush=True)
