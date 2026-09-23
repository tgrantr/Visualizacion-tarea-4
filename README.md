# ¿A dónde migra la gente en Chile?

Visualización de flujos migratorios intercomunales · Censo 2024

## Ver la visualización

**Versión en línea:** https://tgrantr.github.io/Visualizacion-tarea-4/

También se puede abrir `index.html` de forma local con doble clic en Chrome. Es un archivo autónomo (deck.gl embebido) y no requiere internet ni instalación.

## Reproducir el proyecto desde cero

### Requisitos

- Python 3.11 o superior
- Conexión a internet (solo para las descargas iniciales: Censo ~500 MB, GeoJSON de comunas y deck.gl)
- En Windows, clonar el repositorio en una ruta corta (por ejemplo `C:\proyecto`). Las rutas muy largas pueden hacer fallar `pip install` por el límite de 260 caracteres de Windows (`WinError 206`).

### Pasos

```
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Correr el pipeline completo
python pipeline.py
```

El pipeline hace todo automáticamente:

1. Descarga los GeoJSON de comunas y regiones de Chile y la librería deck.gl 8.9.35
2. Descarga el Parquet del Censo 2024 desde el INE (~500 MB, solo la primera vez)
3. Procesa los datos y construye los flujos migratorios
4. Genera la figura estática PNG
5. Genera el HTML animado autónomo, con deck.gl embebido

Todo lo descargado queda en `data/raw/` y se reutiliza en las ejecuciones siguientes.

### Archivos generados

- `outputs/desafio_4.html`: visualización animada
- `index.html`: copia de la anterior en la raíz del repositorio (para GitHub Pages)
- `figures/red_migracion_chile.png`: figura estática a 300 DPI
- `data/processed/flujos.csv`: aristas filtradas
- `data/processed/centroides.csv`: coordenadas de comunas
- `data/processed/comunas_attr.csv`: atributos por comuna

### Publicación en GitHub Pages

El sitio usa el `index.html` de la raíz del repositorio, que el pipeline actualiza en cada ejecución. Para publicar una versión nueva basta con subir ese archivo a GitHub.

El Parquet del Censo no se incluye en el repositorio por su tamaño. El pipeline lo descarga en la primera ejecución.

### Si algo falla

El error más probable es que algún nombre de columna del Parquet difiera del documentado. En ese caso el script imprime las columnas disponibles y hay que ajustarlas en la sección `COLS` de `paso2_procesa()` en `pipeline.py`.

## Contenido del repositorio

- `index.html`: visualización animada publicada
- `pipeline.py`: pipeline completo (descarga, procesamiento y figuras)
- `requirements.txt`: dependencias con versiones fijadas
- `desafio_4_libro_codigos.pdf`: libro de códigos

## Fuentes de datos

- Microdatos Censo 2024: Instituto Nacional de Estadísticas (INE), Chile
- Geodatos de comunas y regiones: alvaroparedesl/geochile (GitHub)
- Motor de visualización: deck.gl 8.9.35
