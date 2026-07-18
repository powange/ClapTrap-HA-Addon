from flask import Blueprint, jsonify, request
import logging
import numpy as np

from settings_manager import load_settings
from routes.sources import _resolve_pulse_name

testing_bp = Blueprint('testing', __name__)
_socketio = None

def init_testing(socketio):
    global _socketio
    _socketio = socketio


# --- Test micro (VU-mètre) ---
_mic_test_thread = None
_mic_test_running = False


@testing_bp.route('/api/mic/test/start', methods=['POST'])
def start_mic_test():
    global _mic_test_thread, _mic_test_running
    if _mic_test_running:
        return jsonify({'success': True, 'message': 'Déjà en cours'})

    settings = load_settings()

    # Utiliser le device envoyé par le frontend (sélection courante) si disponible
    body = request.get_json(silent=True) or {}
    if body.get('pulse_name'):
        pulse_name = body['pulse_name']
        logging.info(f"Test micro: utilisation du device sélectionné: {pulse_name}")
    else:
        pulse_name = _resolve_pulse_name(settings)

    _mic_test_running = True

    def stream_levels():
        global _mic_test_running
        import subprocess as sp
        proc = None
        try:
            # Régler le volume
            mic_volume = settings.get('microphone', {}).get('volume', 100)
            if pulse_name:
                try:
                    sp.run(['pactl', 'set-source-volume', pulse_name, f'{mic_volume}%'],
                           capture_output=True, text=True, timeout=5)
                    logging.info(f"Volume PulseAudio mis à {mic_volume}% pour {pulse_name}")
                except Exception as e:
                    logging.warning(f"Impossible de régler le volume PulseAudio: {e}")

            # Utiliser parecord pour capturer l'audio (bypass sounddevice/PortAudio)
            cmd = ['parecord', '--format=float32le', '--rate=16000', '--channels=1',
                   '--raw', '--latency-msec=50']
            if pulse_name:
                cmd.append(f'--device={pulse_name}')
            logging.info(f"Test micro: lancement de {' '.join(cmd)}")
            proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, bufsize=0)

            import threading as _thr
            _thr.Thread(target=lambda: proc.stderr.read(), daemon=True).start()

            block_size = 800  # 50ms à 16kHz (plus réactif)
            bytes_per_block = block_size * 4
            cb_count = 0

            while _mic_test_running:
                data = proc.stdout.read(bytes_per_block)
                if not data:
                    stderr = proc.stderr.read(4096).decode(errors='replace')
                    logging.error(f"Test micro: parecord a cessé de produire des données. stderr: {stderr}")
                    _socketio.emit('mic_level', {'error': f'parecord: {stderr[:200]}'})
                    break

                samples = np.frombuffer(data, dtype=np.float32)
                peak = float(np.max(np.abs(samples)))
                cb_count += 1
                if cb_count <= 5 or cb_count % 50 == 0:
                    logging.info(f"Test micro #{cb_count}: peak={peak:.6f}, samples={len(samples)}")

                peak_amplified = min(1.0, peak)
                db = max(-60, 20 * np.log10(peak_amplified + 1e-10))
                _socketio.emit('mic_level', {'peak': peak_amplified, 'db': round(db, 1)})

        except Exception as e:
            logging.error(f"Erreur test micro: {e}")
            _socketio.emit('mic_level', {'error': str(e)})
        finally:
            _mic_test_running = False
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()

    _mic_test_thread = _socketio.start_background_task(stream_levels)
    return jsonify({'success': True})


@testing_bp.route('/api/mic/test/stop', methods=['POST'])
def stop_mic_test():
    global _mic_test_running
    _mic_test_running = False
    return jsonify({'success': True})


# --- Test RTSP (VU-mètre) ---
_rtsp_test_thread = None
_rtsp_test_running = False


@testing_bp.route('/api/rtsp/test/start', methods=['POST'])
def start_rtsp_test():
    global _rtsp_test_thread, _rtsp_test_running
    if _rtsp_test_running:
        return jsonify({'success': True, 'message': 'Déjà en cours'})

    data = request.get_json(silent=True) or {}
    rtsp_url = data.get('url', '')
    initial_gain = float(data.get('gain', 10))
    if not rtsp_url:
        return jsonify({'error': 'URL RTSP requise'}), 400
    # Securite : n'accepter que rtsp://. Sans ca, ffmpeg accepterait file://,
    # http://, concat: etc. -> lecture de fichiers locaux / SSRF.
    if '://' in rtsp_url:
        if not rtsp_url.startswith(('rtsp://', 'rtsps://')):
            return jsonify({'error': 'Seul le protocole rtsp:// est autorisé'}), 400
    else:
        rtsp_url = 'rtsp://' + rtsp_url

    # Initialiser le gain dans le dict partagé pour le temps réel
    from classify import _rtsp_gains, update_rtsp_gain
    _rtsp_gains[rtsp_url] = initial_gain

    _rtsp_test_running = True

    def stream_rtsp_levels():
        global _rtsp_test_running
        import subprocess as sp
        proc = None
        try:
            cmd = [
                'ffmpeg',
                '-protocol_whitelist', 'rtsp,rtp,udp,tcp,tls',
                '-i', rtsp_url,
                '-vn',
                '-f', 'f32le', '-acodec', 'pcm_f32le',
                '-ac', '1', '-ar', '16000',
                '-loglevel', 'error',
                'pipe:1'
            ]
            logging.info(f"Test RTSP: ffmpeg {rtsp_url} (volume={initial_gain}x)")
            proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE)

            import threading as _thr
            _thr.Thread(target=lambda: proc.stderr.read(), daemon=True).start()

            block_size = 1600
            bytes_per_block = block_size * 4
            cb_count = 0

            while _rtsp_test_running:
                raw = proc.stdout.read(bytes_per_block)
                if not raw:
                    _socketio.emit('rtsp_level', {'error': 'Flux RTSP interrompu ou URL invalide'})
                    break

                samples = np.frombuffer(raw, dtype=np.float32)
                # Relire le gain en temps réel
                gain = _rtsp_gains.get(rtsp_url, initial_gain)
                peak = float(np.max(np.abs(samples))) * gain
                cb_count += 1
                if cb_count <= 3 or cb_count % 50 == 0:
                    logging.info(f"Test RTSP #{cb_count}: peak={peak:.6f} (gain={gain}x)")

                peak_amplified = min(1.0, peak)
                db = max(-60, 20 * np.log10(peak_amplified + 1e-10))
                _socketio.emit('rtsp_level', {'peak': peak_amplified, 'db': round(db, 1), 'url': rtsp_url})

        except Exception as e:
            logging.error(f"Erreur test RTSP: {e}")
            _socketio.emit('rtsp_level', {'error': str(e)})
        finally:
            _rtsp_test_running = False
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()

    _rtsp_test_thread = _socketio.start_background_task(stream_rtsp_levels)
    return jsonify({'success': True})


@testing_bp.route('/api/rtsp/test/stop', methods=['POST'])
def stop_rtsp_test():
    global _rtsp_test_running
    _rtsp_test_running = False
    return jsonify({'success': True})


# --- Test VBAN (VU-metre via tap sur le detecteur partage) -------------------
@testing_bp.route('/api/vban/test/start', methods=['POST'])
def start_vban_test():
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    if not ip:
        return jsonify({'error': 'IP requise'}), 400
    try:
        from vban_manager import get_vban_detector
        detector = get_vban_detector()
        if detector is None:
            return jsonify({'error': 'VBAN detecteur non initialise'}), 500

        def _tap(peak):
            try:
                import math as _math
                # Appliquer le gain configure en live (memes conditions que la
                # detection en cours) pour que le VU-metre reflete ce que le
                # classifier voit reellement.
                try:
                    from classify import _vban_gains
                    gain = _vban_gains.get(ip)
                except Exception:
                    gain = None
                if gain is None:
                    try:
                        s = load_settings()
                        for src in (s.get('saved_vban_sources') or []):
                            if src.get('ip') == ip:
                                gain = float(src.get('gain', 1.0))
                                break
                    except Exception:
                        gain = 1.0
                gain = gain if gain is not None else 1.0
                peak_capped = min(1.0, peak * gain)
                db = max(-60, 20 * _math.log10(peak_capped + 1e-10))
                _socketio.emit('vban_level', {
                    'ip': ip,
                    'peak': peak_capped,
                    'db': round(db, 1),
                    'gain': gain,
                })
            except Exception:
                pass

        detector.set_test_tap(ip, _tap)
        logging.info(f"VBAN test: tap actif sur {ip}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Erreur start_vban_test: {e}")
        return jsonify({'error': str(e)}), 500


@testing_bp.route('/api/vban/test/stop', methods=['POST'])
def stop_vban_test():
    try:
        from vban_manager import get_vban_detector
        detector = get_vban_detector()
        if detector is not None:
            detector.set_test_tap(None, None)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
