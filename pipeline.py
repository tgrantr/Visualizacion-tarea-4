"""
pipeline.py
Flujos migratorios intercomunales · Chile · Censo 2024

Corre de principio a fin sin intervención manual:
  1. Descarga GeoJSON de comunas y regiones de Chile
  2. Descarga Parquet del Censo 2024 desde el INE
  3. Limpia, filtra y calcula entropía de Shannon por comuna
  4. Construye aristas de flujo migratorio
  5. Exporta CSV de flujos
  6. Genera figura estática PNG
  7. Genera HTML animado con deck.gl TripsLayer

Outputs:
  figures/red_migracion_chile.png
  outputs/flujos_migratorios_chile.html
  data/processed/flujos.csv
  data/processed/comunas_attr.csv
  data/processed/centroides.csv

Supuestos explícitos:
  - Flujos: solo personas que hace 5 años vivían en OTRA comuna de Chile
  - Entropía: solo trabajadores 15-65 con P51_ACTEC_2D válido
  - Umbral: aristas con < 500 personas eliminadas
  - Santiago: flujos recortados al percentil 95
"""

import os
import json
import math
import zipfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import entropy
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.collections import LineCollection

# ─── rutas ───────────────────────────────────────────────────────────────────

BASE      = Path(__file__).parent
RAW       = BASE / "data" / "raw"
PROCESSED = BASE / "data" / "processed"
FIGURES   = BASE / "figures"
OUTPUTS   = BASE / "outputs"

for d in [RAW, PROCESSED, FIGURES, OUTPUTS]:
    d.mkdir(parents=True, exist_ok=True)

# ─── parámetros (todos en un lugar, fácil de ajustar) ────────────────────────

URL_CENSO      = "https://storage.googleapis.com/bktdescargascenso2024/viv_hog_per_censo2024.zip"
URL_COMUNAS    = "https://raw.githubusercontent.com/alvaroparedesl/geochile/master/geojson/chile.geojson"

UMBRAL_FLUJO   = 500
EDAD_MIN       = 15
EDAD_MAX       = 65
COD_RM_PREFIX  = "13"

# deck.gl
LOOP_LENGTH    = 1800
VELOCIDAD      = 12
TRAIL_LENGTH   = 400  # constante: TripsLayer no admite trailLength por-objeto
PARTICULAS_MIN = 1
PARTICULAS_MAX = 20
N_BEZ          = 80


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 1 — DESCARGA DE DATOS
# ═══════════════════════════════════════════════════════════════════════════════

def descargar(url: str, destino: Path, desc: str = "") -> None:
    if destino.exists():
        print(f"  ✓ Ya existe: {destino.name}")
        return
    print(f"  Descargando {desc or destino.name}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        destino.write_bytes(r.read())
    print(f"    → {destino} ({destino.stat().st_size // 1024:,} KB)")


def paso1_descarga():
    print("\n── PASO 1: Descarga de datos ─────────────────────────────────────")

    # GeoJSON comunas (incluye código de región COD_REG por comuna)
    descargar(URL_COMUNAS, RAW / "comunas_chile.geojson", "GeoJSON comunas")

    # Regiones: no existe un geojson de regiones separado en la fuente,
    # así que se derivan disolviendo las comunas por COD_REG.
    regiones_path = RAW / "regiones_chile.geojson"
    if regiones_path.exists():
        print(f"  ✓ Ya existe: {regiones_path.name}")
    else:
        print("  Derivando regiones desde comunas (dissolve por COD_REG)...")
        import geopandas as gpd
        gdf = gpd.read_file(RAW / "comunas_chile.geojson")
        gdf["geometry"] = gdf.geometry.make_valid()
        col_reg = next(c for c in gdf.columns if "cod_reg" in c.lower())
        regiones = gdf.dissolve(by=col_reg, as_index=False)[[col_reg, "geometry"]]
        regiones.to_file(regiones_path, driver="GeoJSON")
        print(f"    → {regiones_path} ({len(regiones)} regiones)")

    # Censo ZIP
    zip_path    = RAW / "censo2024.zip"
    parquet_path = RAW / "personas_censo2024.parquet"

    if not parquet_path.exists():
        if not zip_path.exists():
            print(f"  Descargando Censo 2024 (~500MB)...")
            def progreso(c, bs, total):
                print(f"\r    {min(c*bs/total*100,100):.1f}%", end="", flush=True)
            urllib.request.urlretrieve(URL_CENSO, zip_path, reporthook=progreso)
            print()

        print("  Extrayendo parquet de personas...")
        with zipfile.ZipFile(zip_path) as z:
            candidatos = [n for n in z.namelist()
                          if "per" in n.lower() and n.endswith(".parquet")]
            if not candidatos:
                raise FileNotFoundError(
                    f"Parquet de personas no encontrado en ZIP. "
                    f"Contenido: {z.namelist()}"
                )
            with z.open(candidatos[0]) as src:
                parquet_path.write_bytes(src.read())
        print(f"    → {parquet_path}")
    else:
        print(f"  ✓ Ya existe: personas_censo2024.parquet")


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 2 — PROCESAMIENTO
# ═══════════════════════════════════════════════════════════════════════════════

def zfill5(col: pd.Series) -> pd.Series:
    return col.astype(str).str.strip().str.zfill(5)


def entropia_shannon(grupo: pd.Series) -> float:
    conteos = grupo.value_counts()
    return float(entropy(conteos / conteos.sum()))


def paso2_procesa():
    print("\n── PASO 2: Procesamiento ─────────────────────────────────────────")

    # Cargar solo columnas necesarias
    # Nombres actualizados respecto al esquema documentado en el docstring:
    # el Parquet del Censo 2024 (INE) usa minúsculas y renombró varias
    # preguntas. Mapeo verificado inspeccionando los datos:
    #   REGION, COMUNA, P09           -> region, comuna, edad
    #   P17 / P17_PAIS (resid. hace 5 años, comuna/país)
    #                                 -> p24_lug_resid5 (1=misma vivienda,
    #                                    2=misma comuna, 3=otra comuna de Chile,
    #                                    4=otro país, -99=no aplica)
    #                                    + p24_lug_resid5_esp (comuna/país detalle)
    #   P43_PAGO/P44_NEGO/P45_AGRO (condición de actividad)
    #                                 -> sit_fuerza_trabajo (1=ocupado)
    #   P51_ACTEC_2D (rama activ. econ., 2 díg.)
    #                                 -> cod_caenes (rama CAENES, 1 letra)
    COLS = [
        "region", "comuna",
        "edad",
        "p24_lug_resid5", "p24_lug_resid5_esp",
        "sit_fuerza_trabajo",
        "cod_caenes",
    ]

    print("  Cargando Parquet...")
    try:
        df = pd.read_parquet(RAW / "personas_censo2024.parquet", columns=COLS)
    except Exception as e:
        # Si falla por nombre de columna, mostrar columnas disponibles
        df_all = pd.read_parquet(RAW / "personas_censo2024.parquet")
        raise ValueError(
            f"Error cargando columnas: {e}\n"
            f"Columnas disponibles: {list(df_all.columns)}"
        )

    print(f"    Filas: {len(df):,}")

    df["COMUNA"] = zfill5(df["comuna"])

    # ── entropía ──────────────────────────────────────────────────────────────
    print("  Calculando entropía por comuna...")
    mask_trab = df["sit_fuerza_trabajo"] == 1
    mask_edad = df["edad"].between(EDAD_MIN, EDAD_MAX)
    mask_p51  = df["cod_caenes"].notna() & (df["cod_caenes"] != "999")

    trab = df[mask_trab & mask_edad & mask_p51]
    print(f"    Trabajadores 15-65 con actividad económica válida: {len(trab):,}")

    entropia_com = (
        trab.groupby("COMUNA")["cod_caenes"]
        .apply(entropia_shannon)
        .rename("entropia")
        .reset_index()
    )

    # ── flujos ────────────────────────────────────────────────────────────────
    print("  Construyendo flujos migratorios...")
    # p24_lug_resid5 == 3: vivía en otra comuna de Chile hace 5 años
    mask_mig = df["p24_lug_resid5"] == 3
    migrantes = df.loc[mask_mig, ["p24_lug_resid5_esp", "COMUNA"]].copy()
    migrantes["origen"]  = zfill5(migrantes["p24_lug_resid5_esp"].astype(int))
    migrantes["destino"] = migrantes["COMUNA"]
    migrantes = migrantes[["origen", "destino"]]
    print(f"    Migrantes internos: {len(migrantes):,}")

    flujos = (
        migrantes.groupby(["origen", "destino"])
        .size().reset_index(name="personas")
        .sort_values("personas", ascending=False)
    )

    flujos = flujos[flujos["personas"] >= UMBRAL_FLUJO].copy()
    print(f"    Aristas tras umbral ({UMBRAL_FLUJO}): {len(flujos):,}")

    # Recorte RM al percentil 95
    mask_rm = (
        flujos["origen"].str.startswith(COD_RM_PREFIX) |
        flujos["destino"].str.startswith(COD_RM_PREFIX)
    )
    p95 = int(flujos.loc[mask_rm, "personas"].quantile(0.95))
    flujos.loc[mask_rm & (flujos["personas"] > p95), "personas"] = p95
    print(f"    Flujos RM recortados al p95 ({p95:,} personas)")

    # ── saldo y atributos ─────────────────────────────────────────────────────
    entradas = flujos.groupby("destino")["personas"].sum().rename("entradas")
    salidas  = flujos.groupby("origen")["personas"].sum().rename("salidas")
    saldo = (
        pd.concat([entradas, salidas], axis=1).fillna(0)
        .assign(saldo_neto=lambda x: x["entradas"] - x["salidas"])
        .reset_index().rename(columns={"index": "COMUNA",
                                        "destino": "COMUNA"})
    )
    if "destino" in saldo.columns:
        saldo = saldo.rename(columns={"destino": "COMUNA"})

    comunas_attr = (
        entropia_com.merge(saldo, on="COMUNA", how="outer")
        .fillna(0)
    )

    # ── centroides ────────────────────────────────────────────────────────────
    print("  Calculando centroides...")
    import geopandas as gpd

    gdf = gpd.read_file(RAW / "comunas_chile.geojson")
    posibles = [c for c in gdf.columns
                if any(k in c.lower() for k in ["cod", "com", "cut"])]

    col_cod = None
    for c in posibles:
        muestra = gdf[c].astype(str).str.strip()
        if muestra.str.len().mode()[0] in [4, 5]:
            col_cod = c
            break

    if col_cod is None:
        raise ValueError(
            f"No se encontró columna de código de comuna en el GeoJSON.\n"
            f"Columnas disponibles: {list(gdf.columns)}"
        )

    print(f"    Columna de código usada: {col_cod}")
    gdf["COMUNA"] = zfill5(gdf[col_cod])
    gdf_proj = gdf.to_crs("EPSG:32719")
    gdf["lon"] = gdf_proj.geometry.centroid.to_crs("EPSG:4326").x
    gdf["lat"] = gdf_proj.geometry.centroid.to_crs("EPSG:4326").y
    centroides = gdf[["COMUNA", "lon", "lat"]].copy()

    # ── exportar ──────────────────────────────────────────────────────────────
    flujos.to_csv(PROCESSED / "flujos.csv", index=False)
    comunas_attr.to_csv(PROCESSED / "comunas_attr.csv", index=False)
    centroides.to_csv(PROCESSED / "centroides.csv", index=False)
    print(f"    flujos.csv: {len(flujos):,} aristas")
    print(f"    comunas_attr.csv: {len(comunas_attr):,} comunas")
    print(f"    centroides.csv: {len(centroides):,} comunas")

    return flujos, centroides


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 3 — FIGURA ESTÁTICA
# ═══════════════════════════════════════════════════════════════════════════════

def bezier_pts(p0, p1, n=60):
    dx, dy   = p1[0]-p0[0], p1[1]-p0[1]
    dist     = math.hypot(dx, dy)
    perp     = [-dy/(dist+1e-9), dx/(dist+1e-9)]
    curv     = 0.20
    c1 = [p0[0]+dx*0.33+perp[0]*dist*curv, p0[1]+dy*0.33+perp[1]*dist*curv]
    c2 = [p0[0]+dx*0.66+perp[0]*dist*curv, p0[1]+dy*0.66+perp[1]*dist*curv]
    t  = np.linspace(0, 1, n)[:, None]
    pts = (
        (1-t)**3       * np.array(p0) +
        3*(1-t)**2*t   * np.array(c1) +
        3*(1-t)*t**2   * np.array(c2) +
        t**3           * np.array(p1)
    )
    return pts


def paso3_estatica(flujos, centroides):
    print("\n── PASO 3: Figura estática ───────────────────────────────────────")
    import geopandas as gpd

    coord = centroides.set_index("COMUNA")[["lon", "lat"]].to_dict("index")
    mask  = flujos["origen"].isin(coord) & flujos["destino"].isin(coord)
    flujos = flujos[mask].copy()

    vmin = flujos["personas"].min()
    vmax = flujos["personas"].max()
    flujos["norm"] = (flujos["personas"] - vmin) / (vmax - vmin + 1e-9)

    fig, ax = plt.subplots(figsize=(10, 22), facecolor="black")
    ax.set_facecolor("black")
    ax.set_aspect("equal")

    gpd.read_file(RAW / "comunas_chile.geojson").plot(
        ax=ax, color="#1a1a1a", edgecolor="#3a3a3a", linewidth=0.3
    )
    gpd.read_file(RAW / "regiones_chile.geojson").plot(
        ax=ax, color="none", edgecolor="white", linewidth=0.9
    )

    cmap = mcolors.LinearSegmentedColormap.from_list(
        "flujo", ["#ffff00", "#ff8800", "#cc0000"]
    )

    segmentos, colores = [], []
    for _, row in flujos.sort_values("norm").iterrows():
        o = coord.get(str(row["origen"]).zfill(5))
        d = coord.get(str(row["destino"]).zfill(5))
        if not o or not d:
            continue
        pts  = bezier_pts([o["lon"], o["lat"]], [d["lon"], d["lat"]])
        segs = np.array([pts[:-1], pts[1:]]).transpose(1, 0, 2)
        segmentos.extend(segs)
        colores.extend([cmap(row["norm"])] * len(segs))

    if segmentos:
        lc = LineCollection(segmentos, colors=colores,
                            linewidths=0.6, alpha=0.45, zorder=2)
        ax.add_collection(lc)

    sm = plt.cm.ScalarMappable(
        cmap=cmap, norm=mcolors.Normalize(vmin=int(vmin), vmax=int(vmax))
    )
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.015, pad=0.02, aspect=30)
    cbar.set_label("Personas que migraron", color="white", fontsize=8)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white", fontsize=7)
    cbar.ax.yaxis.set_tick_params(color="white")
    cbar.outline.set_edgecolor("white")

    ax.set_title(
        "Flujos migratorios entre comunas de Chile\n"
        "Censo 2024 · Color = volumen · Solo flujos ≥ 500 personas",
        color="white", fontsize=10, pad=12
    )
    ax.axis("off")
    fig.tight_layout(pad=0.5)

    salida = FIGURES / "red_migracion_chile.png"
    fig.savefig(salida, dpi=300, bbox_inches="tight",
                facecolor="black", edgecolor="none")
    plt.close(fig)
    print(f"  ✓ {salida}")


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 4 — HTML ANIMADO
# ═══════════════════════════════════════════════════════════════════════════════

def bezier_pts_list(p0, p1, n=N_BEZ):
    dx, dy = p1[0]-p0[0], p1[1]-p0[1]
    dist   = math.hypot(dx, dy)
    perp   = [-dy/(dist+1e-9), dx/(dist+1e-9)]
    curv   = 0.20
    c1 = [p0[0]+dx*0.33+perp[0]*dist*curv, p0[1]+dy*0.33+perp[1]*dist*curv]
    c2 = [p0[0]+dx*0.66+perp[0]*dist*curv, p0[1]+dy*0.66+perp[1]*dist*curv]
    pts = []
    for i in range(n):
        t = i / (n-1)
        x = (1-t)**3*p0[0] + 3*(1-t)**2*t*c1[0] + 3*(1-t)*t**2*c2[0] + t**3*p1[0]
        y = (1-t)**3*p0[1] + 3*(1-t)**2*t*c1[1] + 3*(1-t)*t**2*c2[1] + t**3*p1[1]
        pts.append([round(x, 5), round(y, 5)])
    return pts


def norm_a_color(norm):
    if norm < 0.5:
        t = norm * 2
        return [255, int(255*(1-t) + 136*t), 0]
    else:
        t = (norm-0.5) * 2
        return [int(255*(1-t) + 200*t), int(136*(1-t)), 0]


def paso4_animada(flujos, centroides):
    print("\n── PASO 4: HTML animado ──────────────────────────────────────────")

    coord = centroides.set_index("COMUNA")[["lon", "lat"]].to_dict("index")
    mask  = (
        flujos["origen"].isin(coord) &
        flujos["destino"].isin(coord)
    )
    flujos = flujos[mask].copy()
    flujos["origen"]  = flujos["origen"].astype(str).str.zfill(5)
    flujos["destino"] = flujos["destino"].astype(str).str.zfill(5)

    vmin = flujos["personas"].min()
    vmax = flujos["personas"].max()

    print("  Construyendo trips...")
    trips = []
    for _, row in flujos.iterrows():
        o    = coord[row["origen"]]
        d    = coord[row["destino"]]
        norm = (row["personas"] - vmin) / (vmax - vmin + 1e-9)
        pts  = bezier_pts_list([o["lon"], o["lat"]], [d["lon"], d["lat"]])
        n_pts  = len(pts)
        n_part = int(PARTICULAS_MIN + norm * (PARTICULAS_MAX - PARTICULAS_MIN))
        mismo_region = row["origen"][:2] == row["destino"][:2]
        color = [180, 180, 180] if mismo_region else norm_a_color(norm)

        for k in range(n_part):
            offset = int(k * LOOP_LENGTH / n_part)
            path = [
                [pt[0], pt[1], int(offset + i*LOOP_LENGTH/n_pts) % LOOP_LENGTH]
                for i, pt in enumerate(pts)
            ]
            trips.append({
                "path": path, "color": color,
                "origen": row["origen"], "destino": row["destino"],
            })

    print(f"  Trips generados: {len(trips):,}")

    def leer_geojson(path):
        if not path.exists():
            return "null"
        data = json.loads(path.read_text(encoding="utf-8"))
        for f in data.get("features", []):
            f["properties"] = {}
        return json.dumps(data)

    comunas_json  = leer_geojson(RAW / "comunas_chile.geojson")
    regiones_json = leer_geojson(RAW / "regiones_chile.geojson")
    trips_json    = json.dumps(trips)

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Flujos migratorios · Chile · Censo 2024</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#000;overflow:hidden}}
#deck-canvas{{position:absolute;left:0;top:0;width:100%;height:100%}}
#info{{position:absolute;top:16px;left:16px;z-index:9;pointer-events:none;
  max-width:440px;font-family:Helvetica,Arial,sans-serif}}
#titulo-principal{{font-size:20px;font-weight:700;color:#fff;margin-bottom:4px;line-height:1.25}}
#subtitulo{{font-size:11px;font-weight:400;color:rgba(255,255,255,.7)}}
#legend{{position:absolute;bottom:24px;left:16px;z-index:9;
  color:rgba(255,255,255,.7);font-family:Helvetica,Arial,sans-serif;font-size:11px}}
#bar{{width:160px;height:8px;margin:4px 0;border-radius:4px;
  background:linear-gradient(to right,#ffff00,#ff8800,#cc0000)}}
#labels{{display:flex;justify-content:space-between;width:160px}}
#legend .leyenda-tipo{{display:flex;align-items:center;gap:6px;margin-top:6px}}
#legend .punto{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
#legend .punto-gris{{background:rgb(180,180,180)}}
#legend .punto-color{{background:linear-gradient(to right,#ffff00,#ff8800,#cc0000)}}
#control-velocidad{{position:absolute;top:16px;right:16px;z-index:9;width:190px;
  background:rgba(0,0,0,.75);border-radius:8px;padding:10px 12px;
  font-family:Helvetica,Arial,sans-serif}}
#control-velocidad label{{display:flex;justify-content:space-between;
  color:#fff;font-size:11px;margin-bottom:4px}}
#control-velocidad input{{width:100%;cursor:pointer}}
#panel-regiones{{position:absolute;top:68px;right:16px;z-index:9;width:190px;
  max-height:calc(100% - 84px);overflow-y:auto;
  background:rgba(0,0,0,.75);border-radius:8px;padding:12px;
  font-family:Helvetica,Arial,sans-serif}}
#panel-regiones h3{{color:#fff;font-size:12px;font-weight:500;margin-bottom:8px}}
#panel-regiones .botones{{display:flex;gap:6px;margin-bottom:8px}}
#panel-regiones button{{flex:1;background:rgba(255,255,255,.12);color:#fff;
  border:none;border-radius:4px;padding:4px 0;font-size:10px;cursor:pointer}}
#panel-regiones button:hover{{background:rgba(255,255,255,.22)}}
#panel-regiones label{{display:flex;align-items:center;gap:6px;
  color:#fff;font-size:11px;line-height:1.9;cursor:pointer;user-select:none}}
#panel-regiones input{{cursor:pointer}}
</style>
</head>
<body>
<canvas id="deck-canvas"></canvas>
<div id="info">
  <h1 id="titulo-principal">¿A dónde migra la gente en Chile?</h1>
  <p id="subtitulo">Basado en microdatos del Censo 2024 (INE) · Muestra flujos de 500 o más personas entre comunas</p>
</div>
<div id="legend">
  Flujo de personas
  <div id="bar"></div>
  <div id="labels"><span>Menor</span><span>Mayor</span></div>
  <div class="leyenda-tipo"><span class="punto punto-gris"></span>Movimiento dentro de la misma región</div>
  <div class="leyenda-tipo"><span class="punto punto-color"></span>Movimiento entre regiones distintas</div>
</div>
<div id="control-velocidad">
  <label>Velocidad <span id="valor-velocidad">{VELOCIDAD}</span></label>
  <input type="range" id="slider-vel" min="1" max="30" value="{VELOCIDAD}">
</div>
<div id="panel-regiones">
  <h3>Filtrar por región</h3>
  <div class="botones">
    <button id="btn-todas">Todas</button>
    <button id="btn-ninguna">Ninguna</button>
  </div>
  <div id="lista-regiones"></div>
</div>
<script src="https://unpkg.com/deck.gl@8.9.35/dist.min.js"></script>
<script>
const TRIPS    = {trips_json};
const COMUNAS  = {comunas_json};
const REGIONES = {regiones_json};
const LOOP = {LOOP_LENGTH};
let VEL = {VELOCIDAD};

// ─── control de velocidad ───────────────────────────────────────────────────
const sliderVel = document.getElementById('slider-vel');
const valorVel  = document.getElementById('valor-velocidad');
sliderVel.addEventListener('input', ev => {{
  VEL = Number(ev.target.value);
  valorVel.textContent = VEL;
}});

// ─── filtro por región ────────────────────────────────────────────────────
const REGIONES_NOMBRES = {{
  "01":"Tarapacá", "02":"Antofagasta", "03":"Atacama", "04":"Coquimbo",
  "05":"Valparaíso", "06":"O'Higgins", "07":"Maule", "08":"Biobío",
  "09":"La Araucanía", "10":"Los Lagos", "11":"Aysén", "12":"Magallanes",
  "13":"Metropolitana", "14":"Los Ríos", "15":"Arica y Parinacota", "16":"Ñuble"
}};

const regionesActivas = new Set(Object.keys(REGIONES_NOMBRES));
let TRIPS_FILTRADOS = TRIPS;

function recalcularFiltro() {{
  TRIPS_FILTRADOS = TRIPS.filter(d =>
    regionesActivas.has(d.origen.slice(0,2)) &&
    regionesActivas.has(d.destino.slice(0,2))
  );
}}

const listaRegiones = document.getElementById('lista-regiones');
Object.keys(REGIONES_NOMBRES).sort().forEach(cod => {{
  const label = document.createElement('label');
  label.innerHTML = `<input type="checkbox" data-region="${{cod}}" checked> ${{REGIONES_NOMBRES[cod]}}`;
  listaRegiones.appendChild(label);
}});

listaRegiones.addEventListener('change', ev => {{
  const cb = ev.target;
  if (!cb.matches('input[type=checkbox]')) return;
  const cod = cb.dataset.region;
  if (cb.checked) regionesActivas.add(cod); else regionesActivas.delete(cod);
  recalcularFiltro();
}});

document.getElementById('btn-todas').addEventListener('click', () => {{
  listaRegiones.querySelectorAll('input[type=checkbox]').forEach(cb => {{
    cb.checked = true; regionesActivas.add(cb.dataset.region);
  }});
  recalcularFiltro();
}});

document.getElementById('btn-ninguna').addEventListener('click', () => {{
  listaRegiones.querySelectorAll('input[type=checkbox]').forEach(cb => {{
    cb.checked = false; regionesActivas.delete(cb.dataset.region);
  }});
  recalcularFiltro();
}});

const canvas = document.getElementById('deck-canvas');
canvas.width  = window.innerWidth;
canvas.height = window.innerHeight;
window.addEventListener('resize',()=>{{
  canvas.width=window.innerWidth; canvas.height=window.innerHeight;
}});

const {{Deck, MapView, GeoJsonLayer, TripsLayer}} = deck;

const deckgl = new Deck({{
  canvas: 'deck-canvas',
  width: window.innerWidth,
  height: window.innerHeight,
  views: new MapView({{repeat:false}}),
  initialViewState:{{longitude:-71.5,latitude:-35.5,zoom:4.2,pitch:0,bearing:0}},
  controller: true,
  parameters:{{clearColor:[0,0,0,1]}},
  layers:[]
}});

let t = 0;
function frame(){{
  t = (t + VEL) % LOOP;

  deckgl.setProps({{layers:[
    new GeoJsonLayer({{id:'comunas',data:COMUNAS,filled:true,stroked:true,
      getFillColor:[18,18,18,240],getLineColor:[55,55,55,180],
      lineWidthMinPixels:0.4,pickable:false}}),
    new GeoJsonLayer({{id:'regiones',data:REGIONES,filled:false,stroked:true,
      getLineColor:[210,210,210,220],lineWidthMinPixels:1.0,pickable:false}}),
    new TripsLayer({{id:'trips',data:TRIPS_FILTRADOS,
      getPath:      d=>d.path.map(p=>[p[0],p[1]]),
      getTimestamps:d=>d.path.map(p=>p[2]),
      getColor:     d=>d.color,
      getWidth:3,
      opacity:1,
      widthMinPixels:2,
      trailLength:  {TRAIL_LENGTH},
      currentTime:  t,
      shadowEnabled:false,
      updateTriggers:{{getColor:1, currentTime:t}}
    }})
  ]}});
  requestAnimationFrame(frame);
}}

// Esperar que deck.gl inicialice el WebGL context
setTimeout(frame, 300);
</script>
</body>
</html>"""

    salida = OUTPUTS / "flujos_migratorios_chile.html"
    salida.write_text(html, encoding="utf-8")
    print(f"  ✓ {salida} ({salida.stat().st_size/1024/1024:.1f} MB)")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("═" * 60)
    print("  Flujos migratorios · Chile · Censo 2024")
    print("═" * 60)

    paso1_descarga()
    flujos, centroides = paso2_procesa()
    paso3_estatica(flujos, centroides)
    paso4_animada(flujos, centroides)

    print("\n" + "═" * 60)
    print("  COMPLETADO")
    print(f"  PNG  → {FIGURES / 'red_migracion_chile.png'}")
    print(f"  HTML → {OUTPUTS / 'flujos_migratorios_chile.html'}")
    print("═" * 60)
