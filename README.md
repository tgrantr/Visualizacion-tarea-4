# ¿A dónde migra la gente en Chile?
Visualización de flujos migratorios intercomunales · Censo 2024

## Ver la visualización
Abrir `desafio_4.html` con doble clic en Chrome. No requiere internet ni instalación.

## Reproducir el proyecto desde cero

### Requisitos
- Python 3.10 o superior
- Conexión a internet (solo para la descarga inicial del Censo, ~500MB)

### Pasos

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Correr el pipeline completo
python pipeline.py
```

El pipeline hace todo automáticamente:
1. Descarga los GeoJSON de comunas y regiones de Chile
2. Descarga el Parquet del Censo 2024 desde el INE (~500MB, solo la primera vez)
3. Procesa los datos y construye los flujos migratorios
4. Genera la figura estática PNG
5. Genera el HTML animado standalone

### Outputs generados
- `outputs/desafio_4.html` — visualización animada
- `figures/red_migracion_chile.png` — figura estática 300 DPI
- `data/processed/flujos.csv` — aristas filtradas
- `data/processed/centroides.csv` — coordenadas de comunas
- `data/processed/comunas_attr.csv` — atributos por comuna

### Si algo falla
El error más probable es que algún nombre de columna del Parquet difiera
del documentado. En ese caso el script imprime las columnas disponibles
y hay que ajustarlas en la sección COLS de paso2_procesa() en pipeline.py.

## Fuente de datos
- Microdatos Censo 2024: Instituto Nacional de Estadísticas (INE), Chile
- Geodatos comunas y regiones: alvaroparedesl/geochile (GitHub)
- Motor de visualización: deck.gl 8.9.35
