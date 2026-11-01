# Níscalos – Android app

Motor en Python (numpy + Pillow + stdlib) con interfaz Kivy. Calcula un índice 0-100 de idoneidad
para níscalos en Valencia, Castellón, Cuenca y Teruel (meteorología Open-Meteo + relieve + pinares OSM)
y abre un mapa Leaflet en el navegador del móvil (con botón 📍 para ver tu posición).

## Opción A – Compilar en la nube (sin instalar nada)
1. Crea un repositorio en GitHub (puede ser privado) y sube todo el contenido de esta carpeta
   (incluida `.github/workflows/build-apk.yml`), rama `main`.
2. Pestaña **Actions** → "Build APK" → *Run workflow* (se lanza solo al hacer push).
3. Primera compilación: 20-40 min. Al acabar, descarga el artefacto **niscalos-apk** (zip con el .apk).
4. Pásalo al móvil, ábrelo y permite "instalar apps de orígenes desconocidos".

## Opción B – Compilar en local (Linux / WSL2 Ubuntu)
    sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf libtool pkg-config \
         zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev
    pip install --user buildozer cython
    buildozer android debug          # APK en ./bin/
    buildozer android deploy run     # con el móvil conectado por USB y depuración activada

## Probar en el PC antes de compilar
    pip install kivy numpy pillow certifi
    python main.py

## Notas
- Primera ejecución: descarga pinares (Overpass) y ~600 teselas de relieve (~60 MB). Todo queda en caché.
- Mantén la app abierta y la pantalla encendida mientras calcula (la app lo pide automáticamente).
- Open-Meteo gratuito: ~5.000 puntos/hora. Con 150 puntos/provincia sobra.
- Afinar el modelo: constantes `PARAMS`, `WEIGHTS` y `K_*` al inicio de `core.py`.
