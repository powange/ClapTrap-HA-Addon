"""Routes du calibrage d'une source (voir calibration.py)."""
from flask import Blueprint, jsonify, request

import calibration
from routes.sources import ApiError, api_error_response

calibration_bp = Blueprint('calibration', __name__)
calibration_bp.register_error_handler(Exception, api_error_response)


def _body():
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        raise ApiError('JSON trop imbriqué')
    return data if isinstance(data, dict) else {}


def _run(fn, *args, **kwargs):
    try:
        return jsonify({'success': True, **fn(*args, **kwargs)})
    except calibration.CalibrationError as e:
        raise ApiError(str(e), e.status)


@calibration_bp.route('/api/calibration', methods=['GET'])
def calibration_status():
    return jsonify({'success': True, **calibration.status()})


@calibration_bp.route('/api/calibration/start', methods=['POST'])
def calibration_start():
    return _run(calibration.start, _body().get('source'))


@calibration_bp.route('/api/calibration/record', methods=['POST'])
def calibration_record():
    data = _body()
    return _run(calibration.record, data.get('target'), data.get('expected'), data.get('duration'))


@calibration_bp.route('/api/calibration/analyze', methods=['POST'])
def calibration_analyze():
    return _run(calibration.analyze)


@calibration_bp.route('/api/calibration/apply', methods=['POST'])
def calibration_apply():
    data = _body()
    groups = data.get('groups')
    if groups is not None and (not isinstance(groups, list) or not all(isinstance(g, str) for g in groups)):
        raise ApiError('groups : liste de noms de groupes attendue')
    from settings_manager import to_bool
    try:
        gain = to_bool(data.get('gain', True), 'gain')
        peak_ratio = to_bool(data.get('peak_ratio', True), 'peak_ratio')
    except ValueError as e:
        raise ApiError(str(e))
    return _run(calibration.apply, gain=gain, groups=groups, peak_ratio=peak_ratio)


@calibration_bp.route('/api/calibration/cancel', methods=['POST'])
def calibration_cancel():
    return _run(calibration.cancel)


# --- Calibrage du comptage (reglages globaux, voir count_calibration.py) -------
# Meme session que le calibrage d'une source (annulation : /api/calibration/cancel).

@calibration_bp.route('/api/calibration/count/start', methods=['POST'])
def count_calibration_start():
    import count_calibration
    return _run(count_calibration.start, _body().get('source'))


@calibration_bp.route('/api/calibration/count/record', methods=['POST'])
def count_calibration_record():
    import count_calibration
    data = _body()
    return _run(count_calibration.record, data.get('kind'), data.get('duration'))


@calibration_bp.route('/api/calibration/count/analyze', methods=['POST'])
def count_calibration_analyze():
    import count_calibration
    return _run(count_calibration.analyze)


@calibration_bp.route('/api/calibration/count/apply', methods=['POST'])
def count_calibration_apply():
    import count_calibration
    values = _body().get('values')
    if values is not None and not isinstance(values, dict):
        raise ApiError('values : objet attendu')
    return _run(count_calibration.apply, values)


# --- Calibrage de l'arbitrage entre pieces (voir room_calibration.py) ----------

@calibration_bp.route('/api/calibration/rooms', methods=['GET'])
def room_calibration_status():
    import room_calibration
    from settings_manager import load_settings
    return jsonify({'success': True, **room_calibration.status(),
                    'groups': room_calibration.candidates(load_settings())})


@calibration_bp.route('/api/calibration/rooms/start', methods=['POST'])
def room_calibration_start():
    import room_calibration
    return _run(room_calibration.start, _body().get('zone'))


@calibration_bp.route('/api/calibration/rooms/record', methods=['POST'])
def room_calibration_record():
    import room_calibration
    source = _body().get('source')
    if not isinstance(source, str):
        raise ApiError('source : identifiant de source attendu')
    return _run(room_calibration.record, source)


@calibration_bp.route('/api/calibration/rooms/analyze', methods=['POST'])
def room_calibration_analyze():
    import room_calibration
    return _run(room_calibration.analyze)


@calibration_bp.route('/api/calibration/rooms/apply', methods=['POST'])
def room_calibration_apply():
    import room_calibration
    weights = _body().get('weights')
    if weights is not None and not isinstance(weights, dict):
        raise ApiError('weights : objet {source: poids} attendu')
    return _run(room_calibration.apply, weights)


@calibration_bp.route('/api/calibration/rooms/cancel', methods=['POST'])
def room_calibration_cancel():
    import room_calibration
    return _run(room_calibration.cancel)


# --- Sons appris (voir learned.py) ---------------------------------------------
# Apprentissage : meme session que le calibrage (annulation : /api/calibration/cancel).

@calibration_bp.route('/api/learned', methods=['GET'])
def learned_list():
    import learned
    return jsonify({'success': True, 'sounds': learned.list_sounds(),
                    'limits': {'min_examples': learned.MIN_EXAMPLES, 'max_examples': learned.MAX_EXAMPLES,
                               'recommended_examples': learned.RECOMMENDED_EXAMPLES,
                               'max_sounds': learned.MAX_SOUNDS}})


@calibration_bp.route('/api/learned/<sound_id>', methods=['DELETE'])
def learned_delete(sound_id):
    import learned
    return _run(learned.delete, sound_id)


@calibration_bp.route('/api/learned/start', methods=['POST'])
def learned_start():
    import learned
    data = _body()
    sound_id = data.get('sound_id')
    if sound_id is not None and not isinstance(sound_id, str):
        raise ApiError('sound_id : texte attendu')
    return _run(learned.start, data.get('source'), sound_id)


@calibration_bp.route('/api/learned/record', methods=['POST'])
def learned_record():
    import learned
    return _run(learned.record, _body().get('kind'))


@calibration_bp.route('/api/learned/preview', methods=['POST'])
def learned_preview():
    import learned
    return _run(learned.preview)


@calibration_bp.route('/api/learned/save', methods=['POST'])
def learned_save():
    import learned
    return _run(learned.save, _body().get('name'))
