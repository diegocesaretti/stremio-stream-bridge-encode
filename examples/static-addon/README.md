# Add-on Stremio estático para tus videos

Este ejemplo no necesita Node, Docker ni una app de Home Assistant. Es una carpeta de archivos JSON compatible con los endpoints básicos de un add-on Stremio.

## Iniciar en Windows

1. Instalá Python si todavía no está disponible el comando `py`.
2. Hacé doble clic en `start-addon-server.bat`.
3. Permití el puerto TCP 7000 en el Firewall de Windows para la red privada.
4. En la integración usá `http://IP_DE_LA_PC:7000/manifest.json`.

## Editar el catálogo

- `manifest.json`: nombre y catálogos.
- `catalog/<tipo>/<catalogo>.json`: listado visible.
- `meta/<tipo>/<id>.json`: ficha y episodios.
- `stream/<tipo>/<id>.json`: URL directa, `infoHash` o magnet.

Los IDs deben coincidir exactamente entre catalog, meta y stream. En Windows conviene usar IDs simples sin `:` para que también sean nombres de archivo válidos.

## Reemplazos obligatorios

Los hashes, trackers, IP, posters y rutas son ejemplos. Cambialos antes de probar. Los hashes incluidos tienen formato válido, pero no representan videos reales.
