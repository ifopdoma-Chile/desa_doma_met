import psycopg2.extras
from flask import jsonify, current_app

from app.routes import main
from app.db import get_db_connection

CHART_CONFIG = {
    'Temperatura': {'color': '#e74c3c', 'unit': '°C', 'y_unit': 'Temperatura (°C)', 'title': 'Temperatura'},
    'Presión': {'color': '#f39c12', 'unit': 'hPa', 'y_unit': 'Presión (hPa)', 'title': 'Presión'},
    'Humedad': {'color': '#3498db', 'unit': '%', 'y_unit': 'Humedad (%)', 'title': 'Humedad'},
    'Velocidad del Viento': {'color': '#2ecc71', 'unit': 'm/s', 'y_unit': 'Vel. Viento (m/s)', 'title': 'Velocidad del Viento'},
    'Dirección del Viento': {'color': '#9b59b6', 'unit': '°', 'y_unit': 'Dir. Viento (°)', 'title': 'Dirección del Viento'},
    'Ráfaga de Velocidad': {'color': '#27ae60', 'unit': 'm/s', 'y_unit': 'Ráfaga (m/s)', 'title': 'Ráfaga de Velocidad'},
    'Lluvia': {'color': '#1abc9c', 'unit': 'mm', 'y_unit': 'Lluvia (mm)', 'title': 'Lluvia'},
}

VARIABLES = list(CHART_CONFIG.keys())


def parse_lat_lon_str(val):
    """Parsea latitud/longitud que pueden usar coma o punto decimal."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(',', '.'))
    except ValueError:
        return 0.0


def fetch_estaciones_met():
    """Obtiene estaciones meteorológicas IFOP (tipo 2) y Armada (tipo 6).
    Retorna {nivel: [ {nombre, region, lat, lon, codigo} ]}. Si la BD falla, retorna {} (degradación)."""
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT nombre, detalle, latitud, longitud,
                   CASE WHEN tipo = 2 THEN 'IFOP' WHEN tipo = 6 THEN 'Armada' END AS nivel,
                   ubicacionid AS codigo
            FROM estaciones_link2
            WHERE tipo IN (2, 6)
            ORDER BY tipo, nombre
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        estaciones = {}
        for r in rows:
            nivel = r['nivel'] or 'IFOP'
            estaciones.setdefault(nivel, []).append({
                'nombre': str(r['nombre'] or '').strip().title(),
                'region': str(r['detalle'] or '').strip().title(),
                'lat': parse_lat_lon_str(r['latitud']),
                'lon': parse_lat_lon_str(r['longitud']),
                'codigo': r['codigo'],
            })
        return estaciones
    except Exception as e:
        current_app.logger.error(f"Error fetch_estaciones_met: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return {}


@main.route('/estacion_graficos/<int:codigo>')
def get_estacion_graficos(codigo):
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Base de datos no disponible'}), 503

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT nombre, detalle, tipo
            FROM estaciones_link2
            WHERE ubicacionid = %s AND tipo IN (2, 6)
        """, (codigo,))
        row = cur.fetchone()

        if not row:
            cur.close()
            conn.close()
            return jsonify({'error': 'Estación no encontrada'}), 404

        info_estacion = {
            'nombre': str(row['nombre'] or '').strip().title(),
            'region': str(row['detalle'] or '').strip().title(),
            'tipo': 'IFOP' if row['tipo'] == 2 else 'Armada',
            'ultima_medicion': None,
        }

        result_data = {'info_estacion': info_estacion, 'graficos': {}}

        for var in VARIABLES:
            cur.execute("""
                SELECT hora, valor, unidad
                FROM estaciones_valores(%s)
                WHERE nombre = %s
                  AND hora >= NOW() - INTERVAL '168 hours'
                ORDER BY hora ASC
            """, (codigo, var))

            rows = cur.fetchall()
            if not rows:
                continue

            x = [r['hora'].isoformat() for r in rows]
            y = [float(r['valor']) if r['valor'] is not None else None for r in rows]

            # Última medición válida para la barra de estado
            if info_estacion['ultima_medicion'] is None:
                for v, r in zip(y, rows):
                    if v is not None:
                        unidad = r['unidad'] or ''
                        info_estacion['ultima_medicion'] = f"{v:.2f} {unidad}"
                        break

            cfg = CHART_CONFIG[var]
            valores_validos = [v for v in y if v is not None]
            if valores_validos:
                min_y, max_y = min(valores_validos), max(valores_validos)
                margen = max((max_y - min_y) * 0.15, 1.0)
                range_y = [min_y - margen, max_y + margen]
            else:
                range_y = [0, 10]

            result_data['graficos'][var] = {
                'data': [{
                    'x': x,
                    'y': y,
                    'type': 'scatter',
                    'mode': 'lines+markers',
                    'line': {'color': cfg['color'], 'width': 2},
                    'marker': {'size': 4, 'color': cfg['color']},
                    'name': f'{var} ({cfg["unit"]})'
                }],
                'layout': {
                    'margin': {'l': 46, 'r': 12, 't': 8, 'b': 30},
                    'height': 180,
                    'xaxis': {'type': 'date', 'tickformat': '%d/%m\n%H:%M', 'nticks': 8, 'showgrid': False},
                    'yaxis': {'title': cfg['y_unit'], 'color': cfg['color'], 'range': range_y},
                    'plot_bgcolor': 'rgba(0,0,0,0)',
                    'paper_bgcolor': 'rgba(0,0,0,0)',
                    'font': {'size': 10},
                }
            }

        cur.close()
        conn.close()
        return jsonify(result_data)

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500