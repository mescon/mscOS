"""Builds a single-file copy of the product page (pictures inside it, fonts from Google) for
looking at it before it is published. Usage: python3 site/preview.py <out.html>"""
import base64, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
p = open(os.path.join(here, "index.html")).read()
p = re.sub(r'<link rel="preload"[^>]*>\n', "", p)
p = re.sub(r"  @font-face \{[^\n]*\n", "", p)
p = p.replace('"Cormorant", "Cormorant Garamond"', '"Cormorant Garamond"')
p = re.sub(r'src="(img/[a-z0-9-]+\.jpg)"', lambda m: 'src="data:image/jpeg;base64,' + base64.b64encode(open(os.path.join(here, m.group(1)), "rb").read()).decode() + '"', p)
head = re.search(r"<style>.*?</style>", p, re.S).group(0)
body = p[p.index("<body>") + 6:p.index("</body>")]
open(sys.argv[1], "w").write('<title>mscOS product page draft</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;1,400&family=Jost:wght@400;500&display=swap">\n' + head + "\n" + body)
