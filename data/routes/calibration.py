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
    except ValueError as e:
        raise ApiError(str(e))
    return _run(calibration.apply, gain=gain, groups=groups)


@calibration_bp.route('/api/calibration/cancel', methods=['POST'])
def calibration_cancel():
    return _run(calibration.cancel)
