"""Extract translation keys: tr("...") / tr`...` in the panel's JS, static texts in index.html,
and T("...") / N_("...") in the backend (web/backend, next to the static dir).
Usage: i18n-keys.py <static dir> [en.json]  -> prints keys missing from en.json (or all)"""
import re, sys, json, html

root = sys.argv[1]
keys = []

def js_keys(s):
    out = []; i = 0; n = len(s)
    def template(j):  # j after the opening backtick; returns (end, key)
        parts = []; cur = []; idx = 0
        while j < n:
            c = s[j]
            if c == "\\":
                cur.append({"n": "\n", "t": "\t"}.get(s[j+1], s[j+1])); j += 2; continue
            if c == "`": return j + 1, "".join(cur)
            if s.startswith("${", j):
                depth = 1; j += 2
                while depth:
                    if s[j] == "`":
                        j, _ = template(j + 1); continue
                    if s[j] in "\"'":
                        q = s[j]; j += 1
                        while s[j] != q: j += 2 if s[j] == "\\" else 1
                    elif s[j] == "{": depth += 1
                    elif s[j] == "}": depth -= 1
                    j += 1
                cur.append("{%d}" % idx); idx += 1; continue
            cur.append(c); j += 1
        return j, "".join(cur)
    for m in re.finditer(r'\btr\("((?:[^"\\]|\\.)*)"', s):
        out.append(json.loads('"' + m.group(1) + '"'))
    for m in re.finditer(r"\btr`", s):
        _, k = template(m.end()); out.append(k)
    return out

for f in ("app.js", "setup.js"):
    keys += js_keys(open(f"{root}/{f}").read())

import glob, ast
for f in glob.glob(f"{root}/../backend/**/*.py", recursive=True) + glob.glob(f"{root}/../../modules/**/*.py", recursive=True):
    for node in ast.walk(ast.parse(open(f).read())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("T", "N_") and node.args:
            for c in ast.walk(node.args[0]):  # also both arms of T("a" if x else "b")
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    keys.append(c.value)
        # module names and descriptions carry their own translations ({"sv": ..., "en": ...})

s = open(f"{root}/index.html").read()
body = s[s.index("<body"):]
body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
body = re.sub(r"<svg.*?</svg>", "", body, flags=re.S)
for m in re.finditer(r">([^<>]+)<", body):
    t = " ".join(html.unescape(m.group(1)).split())
    if t and re.search(r"[A-Za-zÅÄÖåäö]{2}", t): keys.append(t)
for m in re.finditer(r'(?:aria-label|placeholder|title|alt)="([^"]+)"', body):
    keys.append(html.unescape(m.group(1)))
m = re.search(r'<meta name="description" content="([^"]+)"', s)
if m: keys.append(html.unescape(m.group(1)))

seen = []; [seen.append(k) for k in keys if k not in seen]
have = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else {}
missing = [k for k in seen if k not in have]
unused = [k for k in have if k not in seen]
print(json.dumps(missing, ensure_ascii=False, indent=0))
print(f"# {len(seen)} keys, {len(missing)} missing, {len(unused)} unused", file=sys.stderr)
if unused: print("# unused:", unused[:20], file=sys.stderr)
if "--prune" in sys.argv and unused:  # drop translations of texts that no longer exist
    json.dump({k: v for k, v in have.items() if k in seen}, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
    open(sys.argv[2], "a").write("\n")
    print(f"# pruned {len(unused)}", file=sys.stderr)
