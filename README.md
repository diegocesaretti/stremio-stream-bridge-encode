# Stremio Stream Bridge v0.5.3

Custom Home Assistant integration that combines Stremio-compatible catalogs, stream providers and subtitles, selects practical sources, and sends playback to a configured media player through a compatible stream-server.

## Repository layout

This repository contains only the Home Assistant integration:

```text
custom_components/stremio_stream_bridge
```

The optional Home Assistant Stream Engine app is maintained separately in:

```text
https://github.com/diegocesaretti/stream-server-home-assistant
```

The integration can also use an external stream-server running on a PC. Configure its reachable LAN URL under **Settings → Devices & services → Stremio Stream Bridge → Configure**.

## What is new in 0.5.3

### H.264/x264 name preference

The selector recognizes codec labels already exposed by Stremio add-ons:

```text
Preferred: H.264, H264, x264, AVC
Fallback:  H.265, H265, x265, HEVC
```

When at least one source explicitly names H.264/x264, selection order becomes:

```text
named H.264/x264
→ codec not identified by name
→ named H.265/x265/HEVC
```

H.265 sources are not permanently removed. They remain available as final fallbacks. When no source explicitly names H.264, the previous quality, seed-count and size ranking remains unchanged.

This is a lightweight name filter only. It does not run FFprobe, inspect the real media tracks, remux or transcode the file.

### Direct voice resolver

`stremio_stream_bridge.resolve` is designed for voice clients and automations that should proceed without asking the user to confirm ordinary title or episode choices.

For similar or duplicate movie results, the resolver:

```text
searches plausible titles
→ retrieves streams for each title
→ applies the ideal-link filter
→ selects the result whose accepted source has the most seeders
```

For a series without an explicit episode, it compares available episodes and chooses the episode whose accepted ideal source has the most seeders. Supplying a season limits the comparison to that season.

Explicit requests remain authoritative:

- A supplied year is preserved.
- A supplied season and episode are preserved.
- A nonexistent explicit episode returns `episode_not_found`.
- `profile: sports` remains unsupported for title resolution, while sports catalog browsing and playback continue to work.

Normal resolver outcomes are `exact`, `not_found`, `episode_not_found`, `unsupported` and `error`.

## Home Assistant services

### Search

Searches configured catalogs and optionally returns normalized public results:

```yaml
action: stremio_stream_bridge.search
data:
  query: The Matrix
response_variable: result
```

### Resolve

Resolves a spoken movie or series title without starting playback:

```yaml
action: stremio_stream_bridge.resolve
data:
  query: The Matrix
  media_type: movie
response_variable: result
```

A selected result includes the public media identifiers and may include the winning source seed count:

```yaml
ok: true
status: exact
profile: default
selected:
  media_id: tt0133093
  media_type: movie
  title: The Matrix
  year: 1999
seeders: 120
selection_reason: ideal_stream_seeders
```

Resolve a specific episode:

```yaml
action: stremio_stream_bridge.resolve
data:
  query: Breaking Bad
  media_type: series
  season: 2
  episode: 3
response_variable: result
```

### Play

Starts playback using the selected identifiers:

```yaml
action: stremio_stream_bridge.play
data:
  media_type: "{{ result.selected.media_type }}"
  media_id: "{{ result.selected.media_id }}"
  profile: "{{ result.profile }}"
  media_player: media_player.tv_living
```

## Automatic source selection and fallback

The integration ranks usable sources and can try several automatically. Depending on the configured options, ranking considers:

```text
Cast/direct-play compatibility
→ H.264/x264 name preference
→ preferred resolution
→ highest seed count
→ smallest file when otherwise tied
```

Before each Cast attempt, the integration can stop the previous playback session, close the active Cast receiver, prebuffer a small HTTP range from the new source and wait for the player to reach `playing`. Failed or stalled candidates fall through to the next ranked source.

H.265/x265, MKV, DTS and multichannel sources may still work on some receivers, but are treated conservatively when safer alternatives exist.

## TvOverlay playback status

When enabled, the integration sends a progress message while preparing each ranked source and a success message only after the selected player reports playback:

```text
Buscando una fuente para «The Matrix»… (1/5)
Estás viendo «The Matrix».
```

The configured poster is included when the selected notification service supports images. Notification failures do not interrupt playback.

## Subtitles

The integration can aggregate subtitle providers, download and normalize subtitle files, convert them to WebVTT and temporarily serve them through Home Assistant.

For Home Assistant Cast entities, subtitle styling uses a transparent background and no black edge or window:

```text
edgeType: NONE
background: transparent
window: none
```

## Provider profiles

### Default

Typical defaults:

```text
Catalog and metadata:
https://v3-cinemeta.strem.io/manifest.json

Streams:
https://torrentio.strem.fun/manifest.json

Subtitles:
https://opensubtitles-v3.strem.io/manifest.json
```

### Latin Audio

The Latin profile uses only configured Latin stream providers and disables external subtitles. When a stream-only add-on has no catalogs, the integration mirrors normal movie and series catalogs and queries the Latin provider during playback.

### F1 and Sports

The sports profile creates a dedicated media-browser section from configured sports add-ons. The provider should expose appropriate catalog and stream resources.

## Audio compatibility

Available modes:

- `direct` — sends the original stream-server URL to the player.
- `automatic` — retained for backwards compatibility and currently behaves like `direct`.
- `force_transcode` — explicitly requests the stream-server HLS conversion route and falls back to the original direct URL if conversion fails.

Direct playback remains the safest default because HLS conversion support depends on the configured stream-server build.

## Installation or update

1. Copy `custom_components/stremio_stream_bridge` to `/config/custom_components/`.
2. Replace the existing folder when updating.
3. Restart Home Assistant.
4. Keep the existing configuration entry; no reset is required.
5. Open **Settings → Devices & services → Stremio Stream Bridge → Configure**.
6. Enter a stream-server URL reachable from Home Assistant and the playback device.

Examples:

```text
http://192.168.1.145:11470
http://HOME_ASSISTANT_IP:11470
```

Do not use `127.0.0.1` or `localhost` when the Chromecast or television must open the stream URL itself.

## Internal identity

The Home Assistant integration identity remains:

```text
domain: stremio_stream_bridge
folder: custom_components/stremio_stream_bridge
services: stremio_stream_bridge.*
```

This domain is intentionally unchanged so existing configuration entries, services and automations continue working after the repository rename.

## Notes

- The configured stream-server must be reachable from Home Assistant and the playback device.
- Public Stremio add-ons can change or disappear without notice.
- Optional-provider failures are reported without preventing the core integration from loading.
- Use media, catalogs and providers only where you have the right to access and reproduce the content.
