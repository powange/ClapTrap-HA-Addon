"""Onglet Blueprints : lecture des fichiers livres, etat dans Home Assistant,
ajout et mise a jour (WebSocket simule)."""
import pytest
import yaml

import blueprints


class _InputLoader(yaml.SafeLoader):
    pass


_InputLoader.add_constructor('!input', lambda loader, node: loader.construct_scalar(node))


def test_header_reader_matches_a_real_yaml_parser():
    items = blueprints.bundled()
    assert len(items) >= 6
    for bp in items:
        ref = yaml.load(bp['yaml'], Loader=_InputLoader)['blueprint']
        assert bp['name'] == ref['name'], bp['file']
        assert bp['description'] == ref['description'], bp['file']
        assert bp['source_url'] == ref['source_url'] and bp['source_url'].endswith('/' + bp['file'])
        assert bp['input'] == list(ref.get('input') or {}), bp['file']


class FakeConn:
    def __init__(self, installed=None, fail=None):
        self.installed = dict(installed or {})
        self.calls = []
        self.fail = fail

    def call(self, payload):
        self.calls.append(payload)
        if self.fail and payload['type'] == self.fail:
            return {'success': False, 'error': {'code': 'x', 'message': 'refusé'}}
        if payload['type'] == 'blueprint/list':
            return {'success': True, 'result': self.installed}
        if payload['type'] == 'blueprint/save':
            path = payload['path'] + '.yaml'
            if path in self.installed and not payload.get('allow_override'):
                return {'success': False, 'error': {'code': 'already_exists', 'message': 'File already exists'}}
            meta = blueprints.parse_header(payload['yaml'])
            self.installed[path] = {'metadata': {'name': meta['name'], 'description': meta['description'],
                                                 'source_url': payload['source_url'],
                                                 'input': {k: {} for k in meta['input']}}}
            return {'success': True, 'result': {'overrides_existing': bool(payload.get('allow_override'))}}
        return {'success': True, 'result': None}

    def close(self):
        pass


@pytest.fixture
def ha(settings_dir, monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(blueprints, '_connect', lambda: conn)
    return conn


def _by_file(items):
    return {b['file']: b for b in items}


def test_add_then_up_to_date_then_update(ha):
    items, error = blueprints.list_blueprints()
    assert error is None and {b['status'] for b in items} == {'absent'}
    out = blueprints.install('claps_actions.yaml')
    save = next(c for c in ha.calls if c['type'] == 'blueprint/save')
    assert save['path'] == 'claptrap/claps_actions.yaml'[:-5] and save['allow_override'] is False
    assert save['source_url'].endswith('/claps_actions.yaml') and 'blueprint:' in save['yaml']
    assert out == {'file': 'claps_actions.yaml', 'status': 'a_jour', 'path': 'claptrap/claps_actions.yaml', 'updated': False}
    assert _by_file(blueprints.list_blueprints()[0])['claps_actions.yaml']['status'] == 'a_jour'
    # Nouvelle version livree (autre empreinte) : mise a jour proposee, au meme chemin.
    real = blueprints.bundled

    def newer():
        return [dict(b, sha='autre') if b['file'] == 'claps_actions.yaml' else b for b in real()]
    blueprints.bundled = newer
    try:
        assert _by_file(blueprints.list_blueprints()[0])['claps_actions.yaml']['status'] == 'mise_a_jour'
        ha.calls.clear()
        assert blueprints.install('claps_actions.yaml')['updated'] is True
        save = next(c for c in ha.calls if c['type'] == 'blueprint/save')
        assert save['allow_override'] is True and save['path'] == 'claptrap/claps_actions'
        assert {'type': 'call_service', 'domain': 'automation', 'service': 'reload'} in ha.calls
        assert _by_file(blueprints.list_blueprints()[0])['claps_actions.yaml']['status'] == 'a_jour'
    finally:
        blueprints.bundled = real


def test_blueprint_imported_from_the_docs_is_recognised_and_kept_in_place(ha):
    """Importe par le bouton de la documentation (chemin powange/…) : reconnu
    par son source_url, a jour si HA en montre la meme chose, mis a jour a son
    chemin (les automatisations qui l'utilisent le gardent)."""
    bp = _by_file(blueprints.bundled())['claps_basculer_lumiere.yaml']
    ha.installed['powange/claps_basculer_lumiere.yaml'] = {'metadata': {
        'name': bp['name'], 'description': bp['description'], 'source_url': bp['source_url'],
        'input': {k: {} for k in bp['input']}}}
    assert _by_file(blueprints.list_blueprints()[0])['claps_basculer_lumiere.yaml']['status'] == 'a_jour'
    ha.installed['powange/claps_basculer_lumiere.yaml']['metadata']['description'] = 'Ancienne description'
    item = _by_file(blueprints.list_blueprints()[0])['claps_basculer_lumiere.yaml']
    assert item['status'] == 'different' and item['path'] == 'powange/claps_basculer_lumiere.yaml'
    blueprints.install('claps_basculer_lumiere.yaml')
    save = next(c for c in ha.calls if c['type'] == 'blueprint/save')
    assert save['path'] == 'powange/claps_basculer_lumiere' and save['allow_override'] is True
    assert 'claptrap/claps_basculer_lumiere.yaml' not in ha.installed


def test_status_different_and_reinstall_rules(ha):
    # Meme nom de fichier sous claptrap/, autre origine : « different », remplacable.
    ha.installed['claptrap/claps_assist.yaml'] = {'metadata': {'name': 'Copie', 'source_url': 'https://ailleurs/x.yaml'}}
    item = _by_file(blueprints.list_blueprints()[0])['claps_assist.yaml']
    assert (item['status'], item['path']) == ('different', 'claptrap/claps_assist.yaml')
    blueprints.install('claps_assist.yaml')
    save = next(c for c in ha.calls if c['type'] == 'blueprint/save')
    assert save['allow_override'] is True
    # Installe par ClapTrap puis modifie dans HA (ou sauvegarde restauree) : « different ».
    ha.installed['claptrap/claps_assist.yaml']['metadata']['description'] = 'modifiee'
    assert _by_file(blueprints.list_blueprints()[0])['claps_assist.yaml']['status'] == 'different'


def test_first_install_does_not_reload_automations(ha):
    blueprints.install('claps_confirmation_vocale.yaml')
    assert not [c for c in ha.calls if c['type'] == 'call_service']


def test_errors(ha, monkeypatch):
    with pytest.raises(blueprints.BlueprintError) as e:
        blueprints.install('inconnu.yaml')
    assert e.value.status == 404
    ha.fail = 'blueprint/save'
    with pytest.raises(blueprints.BlueprintError, match='refusé'):
        blueprints.install('claps_actions.yaml')
    # Home Assistant injoignable : liste quand meme, etat inconnu, et l'erreur.
    def down():
        raise blueprints.BlueprintError('Home Assistant injoignable', 503)
    monkeypatch.setattr(blueprints, '_connect', down)
    items, error = blueprints.list_blueprints()
    assert error == 'Home Assistant injoignable' and items and {b['status'] for b in items} == {'inconnu'}
    # Echec de connexion ou d'authentification (RegistryError) : meme chose, pas une erreur 500.
    import ha_registry
    monkeypatch.undo()
    monkeypatch.setattr(ha_registry, '_connect', lambda: (_ for _ in ()).throw(ha_registry.RegistryError('authentification refusée')))
    items, error = blueprints.list_blueprints()
    assert error == 'authentification refusée' and items


def test_reload_failure_does_not_fail_the_install(ha):
    blueprints.install('claps_assist.yaml')
    ha.fail = 'call_service'
    assert blueprints.install('claps_assist.yaml')['status'] == 'a_jour'   # mise a jour : rechargement en echec, sans erreur


def test_routes(settings_dir, monkeypatch):
    from flask import Flask
    from routes.settings_routes import settings_bp
    monkeypatch.delenv('SUPERVISOR_TOKEN', raising=False)
    app = Flask(__name__)
    app.register_blueprint(settings_bp)
    c = app.test_client()
    r = c.get('/api/blueprints')
    assert r.status_code == 200 and r.json['error'] and r.json['blueprints']   # hors de l'add-on
    assert c.post('/api/blueprints/install', json={}).status_code == 400
    assert c.post('/api/blueprints/install', json={'file': 'claps_actions.yaml'}).status_code == 503
    conn = FakeConn()
    monkeypatch.setattr(blueprints, '_connect', lambda: conn)
    r = c.post('/api/blueprints/install', json={'file': 'claps_actions.yaml'})
    assert r.status_code == 200 and r.json['status'] == 'a_jour'
