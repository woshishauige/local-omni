import urllib.request, urllib.parse, re

QUERIES = [
    "大学生 副业 高收益 零成本",
    "大学生 轻松 赚钱 方法 副业",
    "大学生 副业 排名 收益 高收益",
    "大学生 副业 不累 高收益 方法",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")

def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()

def ddg(q):
    try:
        html = fetch("https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(q))
        res = []
        for m in re.finditer(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', html):
            res.append((m.group(1), clean(m.group(2))))
        return res
    except Exception as e:
        return [("ERR", str(e))]

def bing(q):
    try:
        html = fetch("https://www.bing.com/search?q=" + urllib.parse.quote(q) + "&setlang=zh-hans")
        res = []
        for m in re.finditer(r'<h2 class="...[^>]*>(.*?)</h2>.*?<a class="...[^>]*href="([^"]+)"', html, re.S):
            res.append((m.group(2), clean(m.group(1))))
        return res
    except Exception as e:
        return [("ERR", str(e))]

out = []
for q in QUERIES:
    out.append("\n\n### 查询: %s\n" % q)
    r = ddg(q)
    if not r or r[0][0] == "ERR":
        r = bing(q)
    if not r:
        out.append("  (无结果)\n")
        continue
    for i, (u, t) in enumerate(r[:12], 1):
        if not t:
            continue
        out.append("%d. %s\n   %s\n" % (i, t, u))
    out.append("")

open("search_results.txt", "w", encoding="utf-8").write("".join(out))
print("done", len(out))
