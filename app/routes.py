from flask import Blueprint, session, request, jsonify, Response, get_flashed_messages, flash
import requests
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

main = Blueprint('main', __name__)

GEOSERVER_URL = "https://gis-eco.ifop.cl/geoserver"
GEOSERVER_USER = "agarcia"
GEOSERVER_PASS = "dream2004"

@main.before_app_request
def _init_map_session_defaults():
    if 'center' not in session:
        session['center'] = [-30, -72]
    if 'zoom' not in session:
        session['zoom'] = 4

@main.route('/actualizar_mapa', methods=['POST'])
def actualizar_mapa():
    data = request.json
    center = data.get('center', [-30, -72])
    zoom = data.get('zoom', 4)
    session['center'] = center
    session['zoom'] = zoom
    flash(f'Vista actualizada. Centro: [{center[0]:.5f}, {center[1]:.5f}] | Zoom: {zoom}', 'info')
    messages = get_flashed_messages(with_categories=True)
    return jsonify({'status': 'success', 'center': center, 'zoom': zoom, 'messages': messages})

@main.route('/proxy_wms_featureinfo')
def proxy_wms_featureinfo():
    url_wms = f"{GEOSERVER_URL}/Ifop_Sapo/wms"
    allowed = ['SERVICE', 'VERSION', 'REQUEST', 'LAYERS', 'QUERY_LAYERS',
               'STYLES', 'SRS', 'BBOX', 'WIDTH', 'HEIGHT', 'X', 'Y',
               'INFO_FORMAT', 'FEATURE_COUNT', 'TILED']
    params = {k: v for k, v in request.args.items() if k.upper() in allowed}
    try:
        backend_r = requests.get(url_wms, params=params, timeout=10)
        resp = Response(backend_r.content, status=backend_r.status_code,
                        content_type=backend_r.headers.get('content-type', 'application/json'))
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as e:
        logger.error(f"Error en proxy WMS: {e}")
        return jsonify({'error': str(e)}), 502

from app import routes_doma_met
