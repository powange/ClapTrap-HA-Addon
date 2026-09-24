"""Rend templates/index.html avec les scripts integres, pour ui_test.js."""
import json, os, re, sys
from jinja2 import Environment, FileSystemLoader
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.dirname(os.path.dirname(HERE))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'page.html')
env = Environment(loader=FileSystemLoader(os.path.join(DATA, 'templates')))
env.globals['url_for'] = lambda endpoint, filename='': '/static/' + filename
settings = json.load(open(os.path.join(HERE, 'settings_ui.json')))
html = env.get_template('index.html').render(settings=settings, devices=[{'index': 2, 'name': 'USB', 'pulse_name': 'u'}, {'index': 5, 'name': 'A|B', 'pulse_name': 'x'}], ingress_path='', cache_bust='1', advanced_defaults={'delay': 1.5, 'peak_cooldown': 0.08, 'peak_ratio': 3.0})
def inline(m):
    name = m.group(1)
    return '<script>' + open(os.path.join(DATA, 'static', 'js', name)).read() + '\n</script>'
html = re.sub(r'<script src="/js/1/([a-z_]+\.js)"></script>', inline, html)
html = re.sub(r'<script src="/static/js/vendor/socket.io.min.js"></script>', '', html)
open(OUT, 'w').write(html)
