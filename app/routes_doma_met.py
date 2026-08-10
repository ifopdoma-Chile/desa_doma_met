from flask import render_template, session, current_app, jsonify
import os
import time
import folium
from folium.plugins import MousePosition
from folium import plugins
import numpy as np
import json
from io import BytesIO
import rasterio
from datetime import datetime
import requests
import xml.etree.ElementTree as ET
from requests.auth import HTTPBasicAuth
from app.routes import main, GEOSERVER_URL, GEOSERVER_USER, GEOSERVER_PASS, logger
import pytz

maxzoom = 12
minzoom = 3
chile_tz = pytz.timezone('America/Santiago')
GEOSERVER_WMS_URL = f"{GEOSERVER_URL}/Ifop_Sapo/wms"

def get_wms_date(layer_name="presatm2"):
    try:
        url = f'{GEOSERVER_URL}/Ifop_Sapo/wms?service=WMS&request=GetCapabilities'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {'wms': 'http://www.opengis.net/wms'}
        for layer in root.findall(".//wms:Layer", ns):
            name = layer.find("wms:Name", ns)
            if name is not None and name.text == layer_name:
                dim = layer.find("wms:Dimension", ns)
                if dim is not None:
                    raw = dim.text.strip()
                    dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ")
                    return dt.strftime("%d/%m/%Y")
    except Exception as e:
        logger.error(f"Error obteniendo fecha WMS {layer_name}: {e}")
    return "Desconocida"

def get_nubes_date():
    chile_tz = pytz.timezone('America/Santiago')
    url = f"{GEOSERVER_URL}/Ifop_Sapo/wms?service=WMS&request=GetCapabilities"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {'wms': 'http://www.opengis.net/wms'}
        for layer in root.findall(".//wms:Layer", ns):
            name = layer.find("wms:Name", ns)
            if name is not None and name.text in ["Nubes_v2", "Ifop_Sapo:Nubes_v2"]:
                dim = layer.find("wms:Dimension", ns)
                if dim is not None and dim.attrib.get("name") == "time":
                    raw = dim.text.strip()
                    if "," in raw:
                        last_time = raw.split(",")[-1].strip()
                    elif "/" in raw:
                        parts = raw.split("/")
                        last_time = parts[1].strip() if len(parts) >= 2 else parts[0].strip()
                    else:
                        last_time = raw
                    try:
                        dt_utc = datetime.fromisoformat(last_time.replace("Z", ""))
                        if dt_utc.year > 3000 or dt_utc.year < 2000:
                            return "Sin fecha"
                        dt_utc = dt_utc.replace(tzinfo=pytz.utc)
                        dt_chile = dt_utc.astimezone(chile_tz)
                        return dt_chile.strftime("%d/%m/%Y")
                    except:
                        return last_time
        return "Sin fecha"
    except Exception as e:
        logger.error(f"Error obteniendo fecha Nubes: {e}")
        return "Sin fecha"


def get_precip_fc_times():
    """Obtiene la lista de tiempos (dimensión time) de la capa Precip_GFS_FC_v1."""
    try:
        url = f'{GEOSERVER_URL}/Ifop_Sapo/wms?service=WMS&request=GetCapabilities'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {'wms': 'http://www.opengis.net/wms'}
        for layer in root.findall(".//wms:Layer", ns):
            name = layer.find("wms:Name", ns)
            if name is not None and name.text in ["Precip_GFS_FC_v1", "Ifop_Sapo:Precip_GFS_FC_v1"]:
                dim = layer.find("wms:Dimension", ns)
                if dim is not None and dim.attrib.get("name") == "time":
                    raw = dim.text.strip()
                    times = [t.strip() for t in raw.split(",") if t.strip()]
                    return times
        return []
    except Exception as e:
        logger.error(f"Error obteniendo tiempos Precip_GFS_FC_v1: {e}")
        return []


def downsample_grid(data, factor):
    """Reduce resolucion de un grid 2D promediando bloques factor x factor"""
    ny, nx = data.shape
    ny2 = ny // factor
    nx2 = nx // factor
    data = data[:ny2*factor, :nx2*factor]
    return data.reshape(ny2, factor, nx2, factor).mean(axis=(1, 3))


def fetch_wind_from_wcs():
    try:
        app_static = os.path.join(current_app.root_path, 'static')
        os.makedirs(app_static, exist_ok=True)

        json_path = os.path.join(app_static, 'wind_data_latest.json')
        metadata_path = os.path.join(app_static, 'wind_metadata.json')

        # Option A: Cache - no regenerar si el JSON tiene menos de 1 hora
        if os.path.exists(json_path):
            mtime = os.path.getmtime(json_path)
            age = time.time() - mtime
            if age < 3600:
                logger.info(f"Viento desde cache ({(age/60):.0f} min de antiguedad)")
                with open(json_path) as f:
                    data = json.load(f)
                if data and len(data) >= 2 and 'header' in data[0]:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    return metadata, True

        auth = HTTPBasicAuth(GEOSERVER_USER, GEOSERVER_PASS)
        wcs_url = f"{GEOSERVER_URL}/Ifop_Sapo/wcs"

        latest_time = None
        try:
            caps_params = {'service': 'WMS', 'request': 'GetCapabilities', 'version': '1.3.0'}
            caps_r = requests.get(GEOSERVER_WMS_URL, params=caps_params, auth=auth, timeout=30, verify=False)
            if caps_r.status_code == 200:
                import re
                for layer in ['u10', 'v10']:
                    pattern = rf'<Layer>.*?<Name>{layer}</Name>.*?<Dimension[^>]*name="time"[^>]*>(.*?)</Dimension>'
                    match = re.search(pattern, caps_r.text, re.DOTALL)
                    if match:
                        dim_text = match.group(1).strip()
                        times = [t.strip() for t in dim_text.split(',') if t.strip()]
                        default_match = re.search(r'default="([^"]+)"', match.group())
                        if times:
                            latest_time = times[-1]
                        elif default_match:
                            latest_time = default_match.group(1)
                        break
        except Exception:
            pass

        def make_wcs_params(coverage_id):
            return [
                ("service", "WCS"),
                ("version", "2.0.1"),
                ("request", "GetCoverage"),
                ("coverageId", coverage_id),
                ("format", "image/tiff"),
                ("subset", "Long(-180,180)"),
                ("subset", "Lat(-90,90)"),
            ]

        resp_u = requests.get(
            wcs_url, params=make_wcs_params("Ifop_Sapo__u10"),
            auth=auth, timeout=120, verify=False
        )
        resp_u.raise_for_status()

        resp_v = requests.get(
            wcs_url, params=make_wcs_params("Ifop_Sapo__v10"),
            auth=auth, timeout=120, verify=False
        )
        resp_v.raise_for_status()

        with rasterio.open(BytesIO(resp_u.content)) as src_u:
            u = src_u.read(1).astype(np.float64)
            transform_u = src_u.transform

        with rasterio.open(BytesIO(resp_v.content)) as src_v:
            v = src_v.read(1).astype(np.float64)

        u = np.nan_to_num(u, nan=0.0)
        v = np.nan_to_num(v, nan=0.0)

        if u.shape != v.shape:
            raise ValueError(f"Dimensiones inconsistentes: u={u.shape}, v={v.shape}")

        ny_full, nx_full = u.shape

        # Option B: Downsample de 720x1439 a ~180x360 (factor 4)
        factor = 4
        u = downsample_grid(u, factor)
        v = downsample_grid(v, factor)

        ny, nx = u.shape
        dx = abs(transform_u.a) * factor
        dy = abs(transform_u.e) * factor
        lo1 = transform_u.c + (dx / 2)
        la1 = transform_u.f - (dy / 2)
        lo2 = lo1 + nx * dx
        la2 = la1 - ny * dy
        la1 = round(la1, 2)
        lo2 = round(lo2, 2)
        la2 = round(la2, 2)
        dx = round(dx, 4)
        dy = round(dy, 4)

        logger.info(f"Grid original: {ny_full}x{nx_full} -> downsample factor {factor}: {ny}x{nx}")

        if latest_time:
            ref_dt = datetime.strptime(latest_time.replace('Z', '').split('.')[0], "%Y-%m-%dT%H:%M:%S")
            ref_time_iso = ref_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            ref_time_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        header_base = {
            "parameterCategory": 2, "nx": nx, "ny": ny,
            "lo1": lo1, "la1": la1, "lo2": lo2, "la2": la2,
            "dx": dx, "dy": dy,
            "refTime": ref_time_iso, "forecastTime": 0
        }

        u_component = {
            "header": {**header_base, "parameterNumber": 2},
            "data": np.round(u, 3).flatten().tolist()
        }

        v_component = {
            "header": {**header_base, "parameterNumber": 3},
            "data": np.round(v, 3).flatten().tolist()
        }

        with open(json_path, "w") as f:
            json.dump([u_component, v_component], f, separators=(',', ':'))

        if latest_time:
            try:
                ref_dt_utc = datetime.strptime(latest_time.replace('Z', '').split('.')[0], "%Y-%m-%dT%H:%M:%S")
                ref_dt_utc = ref_dt_utc.replace(tzinfo=pytz.utc)
                fecha_local = ref_dt_utc.astimezone(chile_tz)
                fecha_dato_str = fecha_local.strftime("%d/%m/%Y %H:%M")
            except Exception:
                fecha_dato_str = datetime.now(pytz.utc).astimezone(chile_tz).strftime("%d/%m/%Y %H:%M")
        else:
            fecha_dato_str = datetime.now(pytz.utc).astimezone(chile_tz).strftime("%d/%m/%Y %H:%M")

        metadata = {
            "fuente": "WCS GeoServer",
            "fecha_dato": fecha_dato_str,
            "fecha_proceso": datetime.now().isoformat(),
            "geoserver_time": latest_time or "desconocida",
            "shape": f"{ny}x{nx}"
        }
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)

        logger.info(f"Viento desde WCS OK: {nx}x{ny}, fecha={latest_time}")
        return metadata, True

    except Exception as e:
        logger.error(f"Error obteniendo viento desde WCS: {e}")
        return None, False


def fetch_wave_data():
    try:
        app_static = os.path.join(current_app.root_path, 'static')
        json_path = os.path.join(app_static, 'wave_data_latest.json')
        metadata_path = os.path.join(app_static, 'wave_metadata.json')

        if not os.path.exists(json_path):
            logger.error("No existe wave_data_latest.json")
            return None, False

        with open(json_path) as f:
            data = json.load(f)

        if not data or len(data) < 2:
            return None, False

        h = data[0].get('header', {})
        ref_time = h.get('refTime', '')
        nx, ny = h.get('nx', 0), h.get('ny', 0)

        if ref_time:
            try:
                ref_dt = datetime.strptime(ref_time.replace('Z', '').split('.')[0].split('T')[0], "%Y-%m-%d")
                fecha_dato_str = ref_dt.strftime("%d/%m/%Y")
            except:
                fecha_dato_str = ref_time
        else:
            fecha_dato_str = datetime.utcnow().strftime("%d/%m/%Y")

        metadata = {
            "fuente": "wave_data_latest.json",
            "fecha_dato": fecha_dato_str,
            "fecha_proceso": datetime.now().isoformat(),
            "shape": f"{ny}x{nx}"
        }
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)

        logger.info(f"Olas desde JSON OK: {nx}x{ny}, fecha={fecha_dato_str}")
        return metadata, True

    except Exception as e:
        logger.error(f"Error obteniendo olas: {e}")
        return None, False


@main.route('/')
def doma_met():
    center = session.get('center', [-30, -72])
    zoom = session.get('zoom', 4)

    pressure_date = get_wms_date("presatm2")
    nubes_date = get_nubes_date()
    precip_date = get_wms_date("Precip_GFS_v1")
    precip_fc_times = get_precip_fc_times()
    precip_fc_date = "Sin fecha"
    if precip_fc_times:
        try:
            dt_utc = datetime.fromisoformat(precip_fc_times[-1].replace("Z", ""))
            dt_utc = dt_utc.replace(tzinfo=pytz.utc)
            precip_fc_date = dt_utc.astimezone(chile_tz).strftime("%d/%m/%Y %HZ")
        except Exception:
            precip_fc_date = precip_fc_times[-1][:16]
    wind_metadata, wind_available = fetch_wind_from_wcs()
    # Olas deshabilitadas - el JSON de 216MB no se procesa
    wave_metadata, wave_available = None, False

    m = folium.Map(
        location=center,
        zoom_start=zoom,
        tiles=None,
        minZoom=minzoom,
        maxZoom=maxzoom,
        zoomDelta=1,
        zoomSnap=1,
        wheelPxPerZoomLevel=250,
    )

    folium.TileLayer(
        tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
        attr='Esri',
        name='Base',
        control=False,
    ).add_to(m)

    formatter = """
            function(lat, lng) {
                var virtual_lng = lng;
                if (lng > 0 && map.getCenter().lng < -100) { virtual_lng = lng - 360; }
                return `Lat: ${lat.toFixed(5)}, Lng: ${virtual_lng.toFixed(5)}`;
            }
            """
    MousePosition(
        position="bottomleft",
        separator=" | ",
        empty_string="",
        lng_first=False,
        num_digits=4,
        prefix="Coordenadas:",
        formatter=formatter
    ).add_to(m)

    plugins.LocateControl(position='bottomleft').add_to(m)

    folium.WmsTileLayer(
        url=GEOSERVER_WMS_URL,
        layers='Ifop_Sapo:presatm2',
        name=f'Presión Atmosférica ({pressure_date})',
        fmt='image/png',
        transparent=True,
        overlay=True,
        opacity=0.5,
        control=True,
        tileSize=256,
        no_wrap=True,
    ).add_to(m)

    folium.WmsTileLayer(
        url=GEOSERVER_WMS_URL,
        layers='Ifop_Sapo:presatm2',
        styles='6_presatm_iso',
        name='Isolíneas Presión',
        fmt='image/png',
        transparent=True,
        overlay=True,
        control=True,
        opacity=1.0,
        tileSize=256,
        no_wrap=True,
    ).add_to(m)

    folium.WmsTileLayer(
        url=GEOSERVER_WMS_URL,
        layers='Ifop_Sapo:Nubes_v2',
        styles='9_Nubes',
        name=f'Nubes ({nubes_date})',
        fmt='image/png',
        transparent=True,
        overlay=True,
        control=True,
        opacity=0.95,
        tileSize=256,
        no_wrap=True,
    ).add_to(m)

    folium.WmsTileLayer(
        url=GEOSERVER_WMS_URL,
        layers='Ifop_Sapo:Precip_GFS_v1',
        styles='Precip_heatmap',
        name=f'Precipitación ({precip_date})',
        fmt='image/png',
        transparent=True,
        overlay=True,
        control=True,
        opacity=0.85,
        tileSize=256,
        no_wrap=True,
    ).add_to(m)

    folium.WmsTileLayer(
        url=GEOSERVER_WMS_URL,
        layers='Ifop_Sapo:Precip_GFS_FC_v1',
        styles='Precip_heatmap',
        name=f'Precipitación Avance ({precip_fc_date})',
        fmt='image/png',
        transparent=True,
        overlay=True,
        control=True,
        opacity=0.8,
        tileSize=256,
        no_wrap=True,
    ).add_to(m)

    wind_date = wind_metadata["fecha_dato"] if wind_metadata else "Sin fecha"
    wind_date = wind_date.split(" ")[0] if wind_date != "Sin fecha" else "Sin fecha"
    wave_date = "Sin fecha"
    wave_date = wave_date.split(" ")[0] if wave_date != "Sin fecha" else "Sin fecha"

    map_setup_script = f"""
    <script src="https://cdn.jsdelivr.net/npm/leaflet-velocity@1.8.1/dist/leaflet-velocity.min.js"></script>
    <script>
    document.addEventListener("DOMContentLoaded", function() {{
        var mapElement = document.querySelector('.folium-map');
        if (!mapElement) return;
        var map = window[mapElement.id];
        if (!map) return;

        var overlays = {{}};
        for (var id in map._layers) {{
            var layer = map._layers[id];
            if (layer instanceof L.TileLayer.WMS) {{
                if (layer.wmsParams.layers.includes("presatm2") && !layer.wmsParams.styles)
                    overlays["Presión Atmosférica ({pressure_date})"] = layer;
                if (layer.wmsParams.styles && layer.wmsParams.styles.includes("6_presatm_iso"))
                    overlays["Isolíneas Presión"] = layer;
                if (layer.wmsParams.layers.includes("Nubes_v2"))
                    overlays["Nubes ({nubes_date})"] = layer;
                if (layer.wmsParams.layers.includes("Precip_GFS_v1"))
                    overlays["Precipitación ({precip_date})"] = layer;
                if (layer.wmsParams.layers.includes("Precip_GFS_FC_v1"))
                    overlays["Precipitación Avance ({precip_fc_date})"] = layer;
            }}
        }}

        var windPlaceholder = L.featureGroup().addTo(map);
        // var wavePlaceholder = L.featureGroup().addTo(map);

        overlays["Viento ({wind_date})"] = windPlaceholder;
        // overlays["Oleaje ({wave_date})"] = wavePlaceholder;

        L.control.layers(null, overlays, {{collapsed: false}}).addTo(map);

        fetch('/doma_met/static/wind_data_latest.json?t=' + Date.now())
        .then(function(r) {{ return r.json(); }})
        .then(function(windData) {{
            window.windData = windData;

            var windLayer = L.velocityLayer({{
                data: windData, maxVelocity: 20, velocityScale: 0.01,
                displayValues: true,
                displayOptions: {{velocityType: 'Viento', position: 'bottomleft', emptyString: 'Sin datos'}},
                colorScale: ['black','black'],
            }}).addTo(map);
            windPlaceholder.addLayer(windLayer);

            /* var waveParticleLayer = L.velocityLayer({{
                data: waveData, maxVelocity: 10, velocityScale: 0.03,
                particleMultiplier: 0.0005, particleAge: 200, lineWidth: 4,
                displayValues: true,
                displayOptions: {{velocityType: 'Oleaje', position: 'bottomleft', emptyString: 'Sin datos'}},
                colorScale: ["#003f5c","#2f4b7c","#665191","#a05195","#d45087","#f95d6a","#ff7c43","#ffa600"],
            }}).addTo(map);
            wavePlaceholder.addLayer(waveParticleLayer); */
        }}).catch(function(err) {{ console.error("Error:", err); }});

    }});
    </script>
    """

    # Script del slider temporal para Precip_GFS_FC_v1 (avance del pronóstico)
    precip_fc_times_json = json.dumps(precip_fc_times)
    precip_fc_script = """
    <script>
    document.addEventListener("DOMContentLoaded", function() {
        var mapElement = document.querySelector('.folium-map');
        if (!mapElement) return;
        var map = window[mapElement.id];
        if (!map) return;

        var times = __PRECIP_FC_TIMES__;
        if (!times || times.length < 2) return;

        map.options.fadeAnimation = false;

        var fcLayer = null;
        for (var id in map._layers) {
            var layer = map._layers[id];
            if (layer instanceof L.TileLayer.WMS && layer.wmsParams.layers.includes("Precip_GFS_FC_v1")) {
                fcLayer = layer;
                break;
            }
        }
        if (!fcLayer) return;
        // Garantizar que la capa FC esté visible en el mapa (por si el overlay no está marcado)
        if (!map.hasLayer(fcLayer)) { map.addLayer(fcLayer); }

        var idx = 0;
        var playing = false;
        var busy = false;   // true mientras el frame actual aun se esta dibujando
        var timer = null;

        function fmt(t) {
            var d = new Date(t);
            return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
        }

        function setTime(i, waitForPaint) {
            idx = Math.max(0, Math.min(times.length - 1, i));
            if (waitForPaint === true) {
                busy = true;
                // refresca el TIME; cuando terminen de cargar los tiles visibles,
                // se dispara "load" de la capa y avanzamos
                fcLayer.off("load").once("load", function() { busy = false; });
                // IMPORTANTE: setParams SIN segundo argumento (noRedraw) para que
                // redibuje los tiles con el nuevo TIME. Pasar `true` evitaba el
                // redibujado -> la capa nunca cambiaba de frame (bug).
                fcLayer.setParams({TIME: times[idx]});
                // red de seguridad: nunca atascarse mas de 6s en un frame
                setTimeout(function(){ if (busy) busy = false; }, 6000);
            } else {
                fcLayer.setParams({TIME: times[idx]});
            }
            var lbl = document.getElementById("precipFcLabel");
            if (lbl) lbl.textContent = fmt(times[idx]);
            var sld = document.getElementById("precipFcSlider");
            if (sld) sld.value = idx;
        }

        var div = L.DomUtil.create("div", "leaflet-control");
        div.style.cssText = "background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,.3);font-size:12px;min-width:220px;z-index:2000;margin-bottom:26px;";
        div.innerHTML =
            '<div style="font-weight:700;color:#1954A2;margin-bottom:4px;">⏱ Avance Precipitación</div>' +
            '<input type="range" id="precipFcSlider" min="0" max="' + (times.length - 1) + '" value="' + idx + '" style="width:100%;">' +
            '<div id="precipFcLabel" style="margin-top:4px;font-weight:600;">' + fmt(times[idx]) + '</div>' +
            '<button id="precipFcPlay" style="margin-top:4px;width:100%;background:#1954A2;color:#fff;border:none;border-radius:4px;padding:4px;cursor:pointer;">▶ Reproducir</button>';
        div.onclick = function(e) { L.DomEvent.stopPropagation(e); };

        var control = L.control({position: 'bottomleft'});
        control.onAdd = function() { return div; };
        control.addTo(map);

        document.getElementById("precipFcSlider").addEventListener("input", function(e) {
            setTime(parseInt(e.target.value));
        });
        document.getElementById("precipFcPlay").addEventListener("click", function(e) {
            e.stopPropagation();
            if (playing) {
                clearInterval(timer); playing = false;
                document.getElementById("precipFcPlay").textContent = "▶ Reproducir";
            } else {
                playing = true;
                document.getElementById("precipFcPlay").textContent = "⏸ Pausa";
                function advance() {
                    if (!playing) return;
                    // esperar a que el frame actual termine de dibujarse
                    if (busy) { timer = setTimeout(advance, 120); return; }
                    var nxt = (idx >= times.length - 1) ? 0 : idx + 1;
                    setTime(nxt, true);
                    // pequena pausa para percibir el frame
                    timer = setTimeout(advance, 650);
                }
                advance();
            }
        });

        // Mostrar primer tiempo por defecto (el play avanza desde el inicio)
        setTime(0);
    });
    </script>
    """.replace("__PRECIP_FC_TIMES__", precip_fc_times_json)

    click_script = """
    <script>
    document.addEventListener("DOMContentLoaded", function() {
        var mapDiv = document.querySelector('.folium-map');
        if (!mapDiv) return;
        var mapId = mapDiv.id;
        var map = window[mapId];
        if (!map) return;

        var proxyUrl = "/doma_met/proxy_wms_featureinfo";
        var wmsLayer = "Ifop_Sapo:presatm2";

        function buildFeatureInfoParams(latlng) {
            var point = map.latLngToContainerPoint(latlng, map.getZoom());
            var size = map.getSize();
            var bounds = map.getBounds();
            var sw = bounds.getSouthWest();
            var ne = bounds.getNorthEast();
            return {
                SERVICE: 'WMS', REQUEST: 'GetFeatureInfo', VERSION: '1.1.1',
                SRS: 'EPSG:4326',
                BBOX: [sw.lng, sw.lat, ne.lng, ne.lat].join(','),
                WIDTH: size.x, HEIGHT: size.y,
                LAYERS: wmsLayer, QUERY_LAYERS: wmsLayer,
                INFO_FORMAT: 'application/json',
                X: Math.round(point.x), Y: Math.round(point.y)
            };
        }

        async function consultarPresion(latlng) {
            var params = buildFeatureInfoParams(latlng);
            var qs = Object.entries(params).map(function(kv) {
                return kv[0] + "=" + encodeURIComponent(kv[1]);
            }).join("&");
            try {
                var resp = await fetch(proxyUrl + '?' + qs);
                if (!resp.ok) return null;
                var json = await resp.json();
                if (json.features && json.features.length > 0 &&
                    json.features[0].properties && json.features[0].properties.MSLP !== undefined) {
                    var val = json.features[0].properties.MSLP;
                    var num = Number(val);
                    if (num !== 0 && isFinite(num)) return num.toFixed(2) + ' hPa';
                }
            } catch(e) {}
            return null;
        }

        function getWindAtLatLng(latlng) {
            if (!window.windData) return null;
            var uData = window.windData[0];
            var vData = window.windData[1];
            var nx = uData.header.nx, ny = uData.header.ny;
            var lo1 = uData.header.lo1, la1 = uData.header.la1;
            var dx = uData.header.dx, dy = uData.header.dy;
            var i = Math.floor((latlng.lng - lo1) / dx);
            var j = Math.floor((la1 - latlng.lat) / dy);
            if (i < 0 || i >= nx || j < 0 || j >= ny) return null;
            var idx = j * nx + i;
            var u = uData.data[idx], v = vData.data[idx];
            if (u == null || v == null) return null;
            var speed = Math.sqrt(u*u + v*v);
            var dir = Math.atan2(u, v) * (180 / Math.PI);
            dir = (dir + 360) % 360;
            return { speed: speed, direction: dir };
        }

        map.on('click', async function(e) {
            if (window.__medicionActiva) return;
            var presion = await consultarPresion(e.latlng);
            var wind = getWindAtLatLng(e.latlng);
            var content = "";
            if (presion) content += "<b>Presión:</b> " + presion + "<br>";
            if (wind) {
                content += "<b>Viento:</b><br>";
                content += "<b>Velocidad:</b> " + wind.speed.toFixed(2) + " m/s<br>";
                content += "<b>Dirección:</b> " + wind.direction.toFixed(1) + "°";
            }
            if (content !== "") {
                L.popup().setLatLng(e.latlng).setContent(content).openOn(map);
            }
        });

        map.on('moveend', function() {
            var center = map.getCenter();
            var zoom = map.getZoom();
            fetch('/doma_met/actualizar_mapa', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ center: [center.lat, center.lng], zoom: zoom })
            }).catch(function(err) { console.error('Error:', err); });
        });
    });
    </script>
    """

    medicion_script = """
    <style>
    .medicion-control a.activo { background: #1954A2 !important; color: #fff !important; }
    .medicion-icono span { display:flex; align-items:center; justify-content:center; width:26px; height:26px; border-radius:50%; background:#1954A2; color:#fff; font-weight:bold; font-size:13px; border:2px solid #fff; box-shadow:0 1px 4px rgba(0,0,0,.4); }
    .medicion-tooltip .leaflet-tooltip-content { background:#1954A2; color:#fff; border-radius:6px; padding:6px 10px; font-weight:600; }
    .medicion-tooltip .leaflet-tooltip-tip { border-top-color: #1954A2; }
    </style>
    <script>
    document.addEventListener("DOMContentLoaded", function() {
        var mapDiv = document.querySelector('.folium-map');
        if (!mapDiv) return;
        var map = window[mapDiv.id];
        if (!map) return;

        var activo = false;
        var puntos = [];
        var linea = null;
        var marcadores = [];
        var tooltipDist = null;
        var btn = null;
        window.__medicionActiva = false;

        function formatearDistancia(m) {
            if (m >= 1000) return (m / 1000).toFixed(2) + ' km';
            return Math.round(m) + ' m';
        }

        function distanciaTotal() {
            var total = 0;
            for (var i = 1; i < puntos.length; i++) {
                total += map.distance(puntos[i - 1], puntos[i]);
            }
            return total;
        }

        function limpiar() {
            if (linea) { map.removeLayer(linea); linea = null; }
            marcadores.forEach(function(m) { map.removeLayer(m); });
            marcadores = [];
            puntos = [];
            if (tooltipDist) { map.closeTooltip(tooltipDist); tooltipDist = null; }
        }

        function iconoPunto(n) {
            return L.divIcon({
                className: 'medicion-icono',
                html: '<span>' + n + '</span>',
                iconSize: [26, 26],
                iconAnchor: [13, 13]
            });
        }

        function onMapClick(e) {
            if (!activo) return;
            puntos.push(e.latlng);
            var n = puntos.length;
            marcadores.push(L.marker(e.latlng, { icon: iconoPunto(n) }).addTo(map));

            if (puntos.length >= 2) {
                if (linea) map.removeLayer(linea);
                linea = L.polyline(puntos, {
                    color: '#1954A2', weight: 3, opacity: 0.9, dashArray: '6,4'
                }).addTo(map);

                var total = distanciaTotal();
                var texto = '<b>Distancia:</b> ' + formatearDistancia(total);
                if (puntos.length > 2) texto = '<b>Total:</b> ' + formatearDistancia(total);
                if (tooltipDist) map.closeTooltip(tooltipDist);
                tooltipDist = L.tooltip({ permanent: true, direction: 'top', className: 'medicion-tooltip' })
                    .setLatLng(e.latlng)
                    .setContent(texto)
                    .addTo(map);
            }
        }

        function activar() {
            limpiar();
            activo = true;
            window.__medicionActiva = true;
            map.getContainer().style.cursor = 'crosshair';
            map.doubleClickZoom.disable();
            map.on('click', onMapClick);
            if (btn) btn.classList.add('activo');
            L.popup().setLatLng(map.getCenter())
                .setContent('Haz clic en el mapa: punto 1 → punto 2 (puedes seguir agregando). Clic en 📏 o ESC para terminar.')
                .openOn(map);
        }

        function desactivar() {
            activo = false;
            window.__medicionActiva = false;
            map.getContainer().style.cursor = '';
            map.doubleClickZoom.enable();
            map.off('click', onMapClick);
            if (btn) btn.classList.remove('activo');
        }

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && activo) desactivar();
        });

        var ControlMedicion = L.Control.extend({
            options: { position: 'topleft' },
            onAdd: function() {
                var container = L.DomUtil.create('div', 'leaflet-bar medicion-control');
                btn = L.DomUtil.create('a', '', container);
                btn.href = '#';
                btn.title = 'Medir distancia';
                btn.innerHTML = '<i class="fa-solid fa-ruler"></i>';
                btn.style.width = '34px';
                btn.style.height = '34px';
                btn.style.display = 'flex';
                btn.style.alignItems = 'center';
                btn.style.justifyContent = 'center';
                btn.style.fontSize = '16px';
                btn.style.color = '#1954A2';
                btn.style.cursor = 'pointer';
                L.DomEvent.disableClickPropagation(container);
                L.DomEvent.on(btn, 'click', function(ev) {
                    L.DomEvent.stop(ev);
                    if (activo) { desactivar(); limpiar(); }
                    else { activar(); }
                });
                return container;
            }
        });
        new ControlMedicion().addTo(map);
        L.control.scale({ position: 'bottomright', imperial: false, metric: true, maxWidth: 200 }).addTo(map);
    });
    </script>
    """

    temp_path = os.path.join(current_app.root_path, 'static', 'map_doma_met.html')
    m.save(temp_path)

    with open(temp_path, encoding='utf-8') as f:
        mapa_html = f.read()

    mapa_html = mapa_html.replace(
        '<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js"></script>', '')
    mapa_html = mapa_html.replace(
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css"/>', '')

    mapa_html = mapa_html.replace('</body>', map_setup_script + '</body>')
    mapa_html = mapa_html.replace('</body>', precip_fc_script + '</body>')
    mapa_html = mapa_html.replace('</body>', click_script + '</body>')
    mapa_html = mapa_html.replace('</body>', medicion_script + '</body>')

    return render_template('doma_met.html', mapa_html=mapa_html,
                           pressure_date=pressure_date, nubes_date=nubes_date,
                           precip_date=precip_date, precip_fc_date=precip_fc_date,
                           wind_date=wind_date, wave_date=wave_date)
