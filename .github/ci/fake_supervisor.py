"""Faux Supervisor pour la CI (test AppArmor) : chaque requete de bashio ou
de ClapTrap recoit une reponse vide valide, pour que l'add-on demarre
completement hors de Home Assistant."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({'result': 'ok', 'data': {}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _reply

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


ThreadingHTTPServer(('0.0.0.0', 80), Handler).serve_forever()
