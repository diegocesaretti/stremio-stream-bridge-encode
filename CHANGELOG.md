# Changelog

## 0.5.3

- Añade filtros por nombre para `H.264`, `H264`, `x264` y `AVC` frente a `H.265`, `H265`, `x265` y `HEVC`.
- Cuando existe al menos una fuente identificada como H.264, la prioriza antes de calidad, semillas y tamaño.
- Conserva las fuentes H.265/HEVC como último fallback en lugar de eliminarlas definitivamente.
- Si ninguna fuente declara H.264 por nombre, mantiene intacto el orden anterior.

## 0.5.2

- Eliminó las respuestas de confirmación `ambiguous` y `series_needs_episode` del flujo normal del resolver.
- Los títulos duplicados se eligen por las semillas del primer stream aceptado por el filtro de enlace ideal.
- Las series sin episodio resuelven automáticamente el capítulo con más semillas; una temporada indicada limita la búsqueda a esa temporada.
- Los episodios pedidos explícitamente siguen respetándose y un episodio inexistente conserva `episode_not_found`.
- El resolver aplica el mismo algoritmo de enlace ideal antes de comparar semillas, sin modificar `play`, fallback, prebuffer, Cast ni subtítulos.

## 0.5.1

- Added the response-capable public `search` contract while preserving `last_search_query` and `last_search_results`.
- Added `stremio_stream_bridge.resolve` for movie, series and episode resolution by spoken title.
- Added normalized public metadata without internal add-on keys or private manifest URLs.
- Added TvOverlay progress messages through `notify.tvoverlaynotify` while sources are tried and after playback starts.
- Kept the existing playback, fallback, Cast, subtitle, media-source and config-entry migration behavior unchanged.
- Added resolver, service-contract and notification regression tests.

## 0.5.0

- Verifica que el reproductor seleccionado no siga reproduciendo contenido anterior.
- En Google Cast, detiene el medio, cierra la app receptora y espera estado inactivo antes de cada intento.
- Ordena y conserva las mejores fuentes según compatibilidad, calidad seleccionada, semillas y tamaño.
- Prueba automáticamente hasta cinco fuentes, con cantidad y timeout configurables.
- Supervisa también reproducciones iniciadas desde el navegador multimedia de Home Assistant.
- Cancela supervisores anteriores cuando se inicia una nueva reproducción en el mismo dispositivo.
- Crea una notificación persistente de Home Assistant cuando se agotan todas las fuentes.
- Añade notificaciones opcionales mediante TvOverlay con portada.
- Expone el estado de fallback y limpieza Cast en el sensor de conectividad.

## 0.4.4

- Added a Cast compatibility filter, enabled by default. Automatic playback now prefers MP4/H.264/AAC and removes known-incompatible MKV, AVI, HEVC/x265, AV1, DTS, TrueHD, E-AC-3, AC-3 and advertised 5.1/7.1 audio candidates whenever a safer alternative exists.
- Automatic stream labels now show container, video codec and audio codec.
- Added direct torrent prebuffering with a small HTTP Range request before handing the URL to Cast. Dead or stalled candidates fall through to the next ranked source.
- Added `Stop current playback before starting`, enabled by default. The selected player receives `media_stop`, waits briefly for the previous HTTP reader to close, then starts the new stream session.
- Restored the legacy `video/mp4` Cast MIME fallback for an MKV/AVI torrent only when it is the remaining fallback.
- No undocumented stream-server reset endpoint is called; stopping the player plus prebuffering the new URL is the safe cross-version session reset.

## 0.4.3

- Restored direct stream playback as the default and migrated v0.4 automatic entries back to direct.
- Kept `automatic` as a backwards-compatible direct-playback alias.
- Limited `hlsv2` conversion to explicit `force_transcode` mode.
- Added real validation for generated `hlsv2` playlists.
- Falls back to the original direct stream whenever HLS audio conversion fails.

## 0.4.2

- Fixed configured Stremio manifest URLs containing commas being split into invalid fragments.
- Optional subtitle, Latin Audio and Sports provider outages no longer cause the whole options form to fail with `cannot_connect`.
- Added migration version 5, preloading Latin Audio and F1/Sports manifests when older entries stored them as empty.
- Added the stream-server URL to the options form and prefilled this installation's known PC address (`http://192.168.1.145:11470`).
- Added automatic Home Assistant LAN URL recommendation for subtitle delivery.
- Preserved explicitly empty optional provider lists after migration so profiles can still be disabled intentionally.
- Improved configuration errors to distinguish a main stream-server connection failure from invalid core manifests.

## 0.4.1

- Fix HLS/DASH MIME detection: the resolved `.m3u8`/`.mpd` URL now takes precedence over an add-on filename hint.
- Validate proxied HLS manifests before casting and automatically fall back to the next ranked source when a link is dead.
- Sports automatic playback no longer blindly trusts the first stream returned by the add-on.
- Add clearer debug logging for selected stream, MIME type and fallback decisions.

## 0.4.0

- Removed black subtitle outlines/background windows on Google Cast.
- Added automatic H.264/AAC HLS compatibility mode to fix silent audio.
- Added direct ideal-link playback on item selection, enabled by default.
- Added separate configurable Latin Audio and F1/Sports provider profiles.
- Latin and sports profiles automatically disable external subtitles.
- Added sports catalog browsing and Latin catalog mirroring.
- Added profile selection to the `play` action.
- Fixed migration from older config-entry versions.

## 0.3.1

- Fixed external subtitles failing silently.
- Added Home Assistant-hosted temporary WebVTT proxy with CORS.
- Added gzip/ZIP, UTF-8, UTF-16, Windows-1252 and Latin-1 subtitle decoding.
- Added fallback retry without Stremio subtitle extras.
- Marked Cast video playback as BUFFERED instead of LIVE.
- Added subtitle provider errors and Cast compatibility to the connectivity sensor.
- Added an optional LAN subtitle base URL for receivers that cannot resolve Home Assistant automatically.

## 0.3.0

- Added subtitle add-on aggregation and OpenSubtitles v3 default configuration.
- Added preferred subtitle languages and automatic subtitle selection.
- Added optional stream-server WebVTT conversion/proxy.
- Added Google Cast external subtitle-track playback.
- Added per-playback subtitle disable option.
- Added ideal-link filter: prefer 1080p, highest seed count, then smallest file.
- Added migration from v0.2 to subtitle-capable configuration.

## 0.2.0

- Aggregate catalog, metadata and stream providers.
- Default Cinemeta + Torrentio configuration.
- Movies/series hierarchy, seasons and episodes.
- Genre/year filters and pagination.
- Automatic and manual stream selection.
- Search service and forward-compatible native media-source search.
- Migration from v0.1 single-add-on entries.

## 0.1.0

- Initial single add-on and stream-server bridge.
