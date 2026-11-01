"""
core.py - Motor del índice de idoneidad para níscalos (Lactarius deliciosus / sanguifluus).

Solo depende de numpy + Pillow + librería estándar, para poder empaquetarse en Android.
  - Pinares:      OpenStreetMap (Overpass), por provincia.
  - Relieve:      teselas "Terrarium" de AWS (PNG con la altitud codificada en RGB, sin API key).
  - Meteorología: Open-Meteo (sin API key).
  - Mapa:         HTML con Leaflet (se abre en el navegador del móvil).
"""
import csv
import hashlib
import json
import math
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import numpy as np

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _CTX = ssl.create_default_context()

UA = "nisqualo-android/2.0 (hobby mushroom app)"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter"]
TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TZ = 12          # zoom de las teselas de relieve (~29 m/píxel a 40° N)
BATCH = 50

PROVINCES = {"V": ("ES-V", "Valencia"), "CS": ("ES-CS", "Castellón"),
             "CU": ("ES-CU", "Cuenca"), "TE": ("ES-TE", "Teruel")}

# ----------------------------------------------------------------------------
# Parámetros micológicos. Trapecio (a, b, c, d): 0 fuera de [a,d], 1 en [b,c].
# ----------------------------------------------------------------------------
PARAMS = {
    "rain_trigger_mm": (10, 35, 100, 200),
    "soil_moisture": (0.12, 0.22, 0.38, 0.48),
    "soil_temp": (3, 8, 16, 22),
    "tmax": (6, 12, 24, 32),
    "elev": (200, 500, 1300, 1800),
    "slope": (-1, 0, 22, 45),
}
WEIGHTS = {"rain_trigger_mm": 3.0, "soil_moisture": 2.0, "soil_temp": 2.0,
           "tmax": 1.0, "elev": 1.5, "slope": 0.7}
SEASON_LOW = {1: .3, 2: .2, 3: .15, 4: .1, 5: .05, 6: .02, 7: .02, 8: .05,
              9: .5, 10: 1, 11: 1, 12: .7}          # < 900 m
SEASON_HIGH = {1: .1, 2: .05, 3: .05, 4: .05, 5: .05, 6: .05, 7: .1, 8: .4,
               9: 1, 10: 1, 11: .5, 12: .15}        # >= 900 m
K_SOIL_MOIST, K_SOIL_TEMP, K_TMAX = 0.35, 3.0, 4.0


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def http(url, params=None, post=None, timeout=60, retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(post).encode() if post else None
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(30 * (i + 1))
            elif e.code >= 500:
                time.sleep(3 * (i + 1))
            else:
                raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def trap(x, a, b, c, d):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return float("nan")
    if x <= a or x >= d:
        return 0.0
    if b <= x <= c:
        return 1.0
    return (x - a) / (b - a) if x < b else (d - x) / (d - c)


def _agg(arr, fn):
    arr = arr[~np.isnan(arr)]
    return float(fn(arr)) if arr.size else float("nan")


# ----------------------------------------------------------------------------
# Terreno: geometría
# ----------------------------------------------------------------------------
def terrain_from_array(z, res):
    """z: ventana cuadrada impar (norte arriba). res: metros/píxel."""
    n = z.shape[0] // 2
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        elev = float(np.nanmean(z[n - 1:n + 2, n - 1:n + 2]))
        a = z[n - 3:n + 4, n - 3:n + 4]
        dzdx = (np.nanmean(a[:, 4:]) - np.nanmean(a[:, :3])) / (4 * res)   # + sube al este
        dzdy = (np.nanmean(a[:3, :]) - np.nanmean(a[4:, :])) / (4 * res)   # + sube al norte
        tpi = elev - float(np.nanmean(z))
    slope = math.degrees(math.atan(math.hypot(dzdx, dzdy)))
    aspect = math.degrees(math.atan2(-dzdx, -dzdy)) % 360   # hacia dónde mira (0=N, 180=S)
    return {"elev": elev, "slope": slope, "aspect": aspect, "tpi": tpi}


def rel_insolation(lat, target, slope, aspect):
    """Radiación directa en la ladera / en llano, de 9 a 15 h solares, para la fecha."""
    doy = target.timetuple().tm_yday
    decl = math.radians(-23.44 * math.cos(2 * math.pi * (doy + 10) / 365))
    la, s, a = math.radians(lat), math.radians(slope), math.radians(aspect)
    normal = np.array([math.sin(s) * math.sin(a), math.sin(s) * math.cos(a), math.cos(s)])
    tot = flat = 0.0
    for hd in range(-60, 61, 15):
        h = math.radians(hd)
        sun = np.array([-math.cos(decl) * math.sin(h),
                        math.sin(decl) * math.cos(la) - math.cos(decl) * math.sin(la) * math.cos(h),
                        math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(h)])
        if sun[2] <= 0.05:
            continue
        tot += max(0.0, float(normal @ sun))
        flat += sun[2]
    return tot / flat if flat else 1.0


def cardinal(az):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((az + 22.5) % 360) // 45)]


def exposure_label(slope, ins):
    if slope < 5:
        return "flat"
    return "umbría (shady)" if ins < -0.15 else "solana (sunny)" if ins > 0.15 else "mid-exposure"


# ----------------------------------------------------------------------------
# Meteorología -> features -> índice
# ----------------------------------------------------------------------------
def parse_weather(d):
    def arr(x):
        return np.array(x, dtype=float)
    dd, hh = d["daily"], d["hourly"]
    nd = len(dd["time"])

    def daily_mean(x):
        x = arr(x)
        x = np.concatenate([x, np.full(max(0, nd * 24 - x.size), np.nan)])[:nd * 24]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return np.nanmean(x.reshape(nd, 24), axis=1)

    return {
        "dates": list(dd["time"]),
        "precip": arr(dd["precipitation_sum"]).tolist(),
        "tmin": arr(dd["temperature_2m_min"]).tolist(),
        "tmax": arr(dd["temperature_2m_max"]).tolist(),
        "soil_t": daily_mean(hh["soil_temperature_6cm"]).round(2).tolist(),
        "soil_m": daily_mean(hh["soil_moisture_3_to_9cm"]).round(3).tolist(),
    }


def features(w, tiso):
    i = w["dates"].index(tiso)

    def win(key, a, b, fn):
        lo = max(0, i + a)
        return _agg(np.array(w[key][lo:i + b + 1], dtype=float), fn)

    return {
        "rain_trigger_mm": win("precip", -18, -6, np.sum),
        "soil_moisture": win("soil_m", -7, 0, np.mean),
        "soil_temp": win("soil_t", -5, 0, np.mean),
        "tmax": win("tmax", -5, 0, np.mean),
        "tmin_7d": win("tmin", -7, 0, np.min),
    }


def apply_terrain(f, p, target):
    ins = float(np.clip(rel_insolation(p["lat"], target, p["slope"], p["aspect"]) - 1, -0.7, 0.8))
    g = dict(f)
    g["soil_moisture"] = float(np.clip(f["soil_moisture"] * (1 - K_SOIL_MOIST * ins), 0, 0.5))
    g["soil_temp"] = f["soil_temp"] + K_SOIL_TEMP * ins
    g["tmax"] = f["tmax"] + K_TMAX * ins
    tpi = p["tpi"]
    g["tmin_7d"] = float(f["tmin_7d"] - 1.5 * np.clip(-tpi / 60, 0, 1) + 0.5 * np.clip(tpi / 60, 0, 1))
    g.update(elev=p["elev"], slope=p["slope"], ins=ins)
    return g


def score(g, target):
    num = den = 0.0
    for k, w in WEIGHTS.items():
        s = trap(g[k], *PARAMS[k])
        if not math.isnan(s):
            num += w * math.log(max(s, 0.02))
            den += w
    if den == 0:
        return 0.0
    base = math.exp(num / den)
    tmin = g["tmin_7d"]
    frost = 1.0 if (tmin is None or math.isnan(tmin) or tmin >= 0) else max(0.2, 1 + tmin * 0.2)
    season = (SEASON_HIGH if g["elev"] >= 900 else SEASON_LOW)[target.month]
    return round(100 * base * frost * season, 1)


# ----------------------------------------------------------------------------
# Mapa HTML (Leaflet)
# ----------------------------------------------------------------------------
HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Níscalos __DATE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>html,body,#map{height:100%;margin:0}
.panel{background:#fff;padding:8px 10px;border-radius:6px;font:13px sans-serif;box-shadow:0 1px 5px rgba(0,0,0,.4)}
.panel input{width:140px}.loc{font-size:22px;cursor:pointer;background:#fff;border-radius:6px;padding:2px 8px;
box-shadow:0 1px 5px rgba(0,0,0,.4)}</style></head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA=__DATA__;
const col=v=>v>=80?'#1a9850':v>=65?'#a6d96a':v>=45?'#fee08b':v>=25?'#fc8d59':'#d73027';
const topo=L.tileLayer('https://tile.opentopomap.org/{z}/{x}/{y}.png',{maxZoom:17,attribution:'© OpenTopoMap'});
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'});
const sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Esri'});
const map=L.map('map',{layers:[topo]});
L.control.layers({'Topographic':topo,'Streets':osm,'Satellite':sat}).addTo(map);
const markers=DATA.map(r=>{const m=L.circleMarker([r.lat,r.lon],{radius:9,color:'#333',weight:1,fillColor:col(r.score),fillOpacity:.9}).bindPopup(r.popup).addTo(map);m.score=r.score;return m;});
if(DATA.length){map.fitBounds(L.latLngBounds(DATA.map(r=>[r.lat,r.lon])).pad(.1));}else{map.setView([40,-1.5],7);}
const ctl=L.control({position:'bottomleft'});
ctl.onAdd=()=>{const d=L.DomUtil.create('div','panel');
d.innerHTML='<b>🍄 __DATE__</b><br>Min score: <span id="v">0</span><br><input id="s" type="range" min="0" max="90" step="5" value="0">'
+'<br><span style="color:#1a9850">●</span>≥80 <span style="color:#a6d96a">●</span>65+ <span style="color:#fee08b">●</span>45+ <span style="color:#fc8d59">●</span>25+ <span style="color:#d73027">●</span>&lt;25';
L.DomEvent.disableClickPropagation(d);return d;};ctl.addTo(map);
document.getElementById('s').oninput=e=>{const t=+e.target.value;document.getElementById('v').textContent=t;
markers.forEach(m=>{if(m.score>=t){m.addTo(map)}else{map.removeLayer(m)}});};
const loc=L.control({position:'topleft'});
loc.onAdd=()=>{const d=L.DomUtil.create('div','loc');d.innerHTML='📍';L.DomEvent.disableClickPropagation(d);
d.onclick=()=>map.locate({setView:true,maxZoom:15,watch:true});return d;};loc.addTo(map);
let me;map.on('locationfound',e=>{if(me)map.removeLayer(me);me=L.circle(e.latlng,{radius:e.accuracy/2,color:'#1565c0'}).addTo(map);});
</script></body></html>"""


def build_html(rows, target):
    data = [{"lat": round(r["lat"], 5), "lon": round(r["lon"], 5), "score": r["score"], "popup": r["popup"]}
            for r in rows]
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTML.replace("__DATA__", js).replace("__DATE__", target.isoformat())


def _popup(r):
    def fm(x, s):
        return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, s)
    sp = (r["species"] or "").replace("<", "").replace(">", "")
    return (f"<b>Score {r['score']:.0f}</b> · {r['prov']}<br>"
            f"{r['exposure']} ({cardinal(r['aspect'])}), slope {r['slope']:.0f}°<br>"
            f"Elevation {r['elev']:.0f} m · TPI {r['tpi']:+.0f} m<br>"
            f"Rain d-18..-6: {fm(r['rain_trigger_mm'], '.0f')} mm<br>"
            f"Soil moisture*: {fm(r['soil_moisture'], '.2f')}<br>"
            f"Soil temp*: {fm(r['soil_temp'], '.1f')} °C · Tmax*: {fm(r['tmax'], '.1f')} °C<br>"
            f"Tmin 7 d*: {fm(r['tmin_7d'], '.1f')} °C<br><small>*terrain-corrected</small>"
            + (f"<br>OSM species: {sp}" if sp else ""))


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------
class Runner:
    def __init__(self, data_dir, log=print):
        self.dir = Path(data_dir)
        self.cache = self.dir / "cache"
        self.tiles_dir = self.cache / "tiles"
        self.out_dir = self.dir / "out"
        for d in (self.tiles_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._lru = OrderedDict()

    # ---- pinares ---------------------------------------------------------
    def pine_points(self, code, per_province, cell=0.03):
        iso, name = PROVINCES[code]
        cf = self.cache / f"osm_{code}.json"
        if cf.exists():
            raw = json.loads(cf.read_text())
        else:
            q = f"""[out:json][timeout:240];
            area["ISO3166-2"="{iso}"]->.a;
            (way(area.a)["leaf_type"="needleleaved"];
             relation(area.a)["leaf_type"="needleleaved"];
             way(area.a)["species"~"Pinus",i];
             way(area.a)["genus"="Pinus"];);
            out tags center 20000;"""
            raw, err = None, None
            for url in OVERPASS_MIRRORS:
                try:
                    j = json.loads(http(url, post={"data": q}, timeout=300))
                    raw = [(e["center"]["lat"], e["center"]["lon"],
                            e.get("tags", {}).get("species") or e.get("tags", {}).get("genus") or "")
                           for e in j.get("elements", []) if "center" in e]
                    break
                except Exception as ex:  # noqa: BLE001
                    err = ex
            if raw is None:
                raise RuntimeError(f"Overpass failed for {name}: {err}")
            cf.write_text(json.dumps(raw))
        seen = {}
        for lat, lon, sp in raw:
            seen.setdefault((round(lat / cell), round(lon / cell)), (lat, lon, sp))
        pts = list(seen.values())
        random.Random(42).shuffle(pts)
        return [{"lat": a, "lon": b, "species": s, "prov": name} for a, b, s in pts[:per_province]]

    # ---- relieve ---------------------------------------------------------
    @staticmethod
    def _gpx(lat, lon):
        n = 2 ** TZ
        gx = (lon + 180) / 360 * n * 256
        gy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n * 256
        return int(gx), int(gy)

    def _tile_path(self, tx, ty):
        return self.tiles_dir / f"{TZ}_{tx}_{ty}.png"

    def _download_tile(self, t):
        try:
            self._tile_path(*t).write_bytes(http(TERRAIN_URL.format(z=TZ, x=t[0], y=t[1]), timeout=30))
        except Exception:  # noqa: BLE001
            pass

    def _tile(self, tx, ty):
        k = (tx, ty)
        if k in self._lru:
            self._lru.move_to_end(k)
            return self._lru[k]
        arr, p = None, self._tile_path(tx, ty)
        if p.exists():
            try:
                from PIL import Image
                im = np.asarray(Image.open(BytesIO(p.read_bytes())).convert("RGB"), dtype=np.float32)
                arr = im[..., 0] * 256 + im[..., 1] + im[..., 2] / 256 - 32768
            except Exception:  # noqa: BLE001
                arr = None
        self._lru[k] = arr
        if len(self._lru) > 48:
            self._lru.popitem(last=False)
        return arr

    def _window(self, lat, lon, half):
        n = 2 ** TZ
        cx, cy = self._gpx(lat, lon)
        x0, y0 = cx - half, cy - half
        size = 2 * half + 1
        out = np.full((size, size), np.nan, np.float32)
        for ty in range(y0 // 256, (cy + half) // 256 + 1):
            for tx in range(x0 // 256, (cx + half) // 256 + 1):
                t = self._tile(tx % n, ty)
                if t is None:
                    continue
                gx0, gx1 = max(x0, tx * 256), min(cx + half, tx * 256 + 255)
                gy0, gy1 = max(y0, ty * 256), min(cy + half, ty * 256 + 255)
                out[gy0 - y0:gy1 - y0 + 1, gx0 - x0:gx1 - x0 + 1] = \
                    t[gy0 - ty * 256:gy1 - ty * 256 + 1, gx0 - tx * 256:gx1 - tx * 256 + 1]
        return out

    def add_terrain(self, points, half=30):
        n = 2 ** TZ
        needed = set()
        for p in points:
            cx, cy = self._gpx(p["lat"], p["lon"])
            for ty in range((cy - half) // 256, (cy + half) // 256 + 1):
                for tx in range((cx - half) // 256, (cx + half) // 256 + 1):
                    needed.add((tx % n, ty))
        missing = [t for t in needed if not self._tile_path(*t).exists()]
        self.log(f"Terrain: downloading {len(missing)} relief tiles ({len(needed) - len(missing)} cached)...")
        with ThreadPoolExecutor(8) as ex:
            for i, _ in enumerate(ex.map(self._download_tile, missing), 1):
                if i % 50 == 0:
                    self.log(f"  tiles {i}/{len(missing)}")

        cf = self.cache / "terrain.json"
        cache = json.loads(cf.read_text()) if cf.exists() else {}
        points = sorted(points, key=lambda p: tuple(x // 256 for x in self._gpx(p["lat"], p["lon"])))
        out = []
        for i, p in enumerate(points, 1):
            key = f"{p['lat']:.5f},{p['lon']:.5f}"
            if key not in cache:
                z = self._window(p["lat"], p["lon"], half)
                if np.isnan(z[half, half]):
                    cache[key] = None
                else:
                    res = 156543.03392 * math.cos(math.radians(p["lat"])) / n
                    cache[key] = terrain_from_array(z, res)
            if cache[key]:
                out.append({**p, **cache[key]})
            if i % 200 == 0:
                self.log(f"  terrain {i}/{len(points)}")
        cf.write_text(json.dumps(cache))
        return out

    # ---- meteorología ----------------------------------------------------
    def fetch_weather(self, points, past_days, forecast_days):
        sig = "|".join(f"{p['lat']:.4f},{p['lon']:.4f}" for p in points)
        stamp = f"{date.today()}_{datetime.now().hour // 3}_{past_days}_{forecast_days}"
        cf = self.cache / f"wx_{hashlib.md5((sig + stamp).encode()).hexdigest()}.json"
        if cf.exists():
            self.log("Weather: using cache (< 3 h old)")
            return json.loads(cf.read_text())
        results = []
        for i in range(0, len(points), BATCH):
            chunk = points[i:i + BATCH]
            params = {
                "latitude": ",".join(f"{p['lat']:.4f}" for p in chunk),
                "longitude": ",".join(f"{p['lon']:.4f}" for p in chunk),
                "daily": "precipitation_sum,temperature_2m_min,temperature_2m_max",
                "hourly": "soil_temperature_6cm,soil_moisture_3_to_9cm",
                "past_days": past_days, "forecast_days": forecast_days, "timezone": "auto",
            }
            data = json.loads(http(OPEN_METEO, params=params, timeout=90))
            data = [data] if isinstance(data, dict) else data
            results += [parse_weather(d) for d in data]
            self.log(f"Weather: {min(i + BATCH, len(points))}/{len(points)}")
            time.sleep(0.5)
        cf.write_text(json.dumps(results))
        return results

    # ---- pipeline --------------------------------------------------------
    def run(self, provinces, target, per_province=150, min_elev=250, max_elev=1800):
        days_ahead = max(0, (target - date.today()).days)
        days_back = max(0, (date.today() - target).days)
        if days_ahead > 15:
            raise ValueError("Forecast only reaches ~15 days ahead.")
        if days_back > 60:
            raise ValueError("Past dates are limited to the last 60 days.")
        pts = []
        for code in provinces:
            self.log(f"Pine forests: {PROVINCES[code][1]}...")
            p = self.pine_points(code, per_province)
            self.log(f"  {len(p)} sample points")
            pts += p
        pts = self.add_terrain(pts)
        pts = [p for p in pts if min_elev <= p["elev"] <= max_elev]
        self.log(f"{len(pts)} points between {min_elev:.0f} and {max_elev:.0f} m")
        weather = self.fetch_weather(pts, min(92, 30 + days_back), min(16, 7 + days_ahead))

        tiso, rows = target.isoformat(), []
        for p, w in zip(pts, weather):
            if tiso not in w["dates"]:
                continue
            g = apply_terrain(features(w, tiso), p, target)
            r = {**p, **g, "score": score(g, target), "exposure": exposure_label(p["slope"], g["ins"])}
            r["popup"] = _popup(r)
            rows.append(r)
        rows.sort(key=lambda r: -r["score"])

        fields = ["prov", "lat", "lon", "score", "elev", "slope", "aspect", "tpi", "exposure",
                  "rain_trigger_mm", "soil_moisture", "soil_temp", "tmax", "tmin_7d", "species"]
        with open(self.out_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            wr.writeheader()
            for r in rows:
                wr.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})
        (self.out_dir / "index.html").write_text(build_html(rows, target), encoding="utf-8")
        self.log(f"Done: {len(rows)} points. Best score: {rows[0]['score'] if rows else '—'}")
        return rows
