import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
q = "测试"
url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(q)
try:
    r = urllib.request.urlopen(url, timeout=15)
    h = r.read().decode("utf-8", "ignore")
    open("ddg_raw.html", "w", encoding="utf-8").write(h)
    print("LEN", len(h))
    print("STATUS", r.status)
    # show result anchor patterns
    import re
    m = re.findall(r'class="result__a"', h)
    print("result__a count:", len(m))
    m2 = re.findall(r'class="result__a" href="([^"]+)"', h)
    print("href count:", len(m2))
    m3 = re.findall(r'href="[^"]+"[^>]*class="result', h)
    print("alt pattern:", len(m3))
    print("FIRST 2000:")
    print(h[:2000])
except Exception as e:
    print("ERR", type(e).__name__, e)
