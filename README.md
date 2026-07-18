# stremio-stream-bridge-encode

Home Assistant integration for selecting a Stremio source, sending it through a PC-hosted Stream Server, and **always requesting on-the-fly transcoding** before playback.

This repository is the encode-focused variant of Stremio Stream Bridge. Its target is the original Chromecast (1st generation), not modern direct-play devices.

## Playback strategy

The integration deliberately does not choose sources by container or codec name. MKV, MP4, H.264, HEVC, DTS and other advertised formats are treated equally during source selection because the original media is not supposed to reach the Chromecast.

The flow is:

```text
Stremio add-on source
→ Stream Server torrent/HTTP URL
→ forced HLS transcode
→ Chromecast-compatible H.264/AAC target
→ Home Assistant media_player.play_media
```

If an encoded HLS output cannot be created or validated, the integration tries the next ranked source. It **does not fall back to direct playback**, because doing so would reintroduce the exact codec/container incompatibilities this variant is meant to eliminate.

## Chromecast 1st generation target

The requested output contract is intentionally conservative:

- HLS with MPEG-TS segments
- H.264 High Profile, level 4.1 or lower
- maximum 1920×1080 at 30 fps
- maximum 8 Mbit/s video
- 8-bit 4:2:0 pixel format
- AAC-LC stereo, 48 kHz, 192 kbit/s

Google documents H.264 High Profile up to level 4.1 for Chromecast 1st and 2nd generation, with 1080p limited to 30 fps.

See [the detailed encoder contract](docs/chromecast-v1-profile.md).

## Important limitation

Home Assistant can request those limits, but the actual bitstream is produced by FFmpeg inside Stream Server. The currently published `perpetus/stream-server` HLS implementation does not consume every compatibility query parameter and may use encoder defaults outside the Chromecast 1st generation envelope.

Therefore:

- this integration is now encode-first and never intentionally sends the original torrent file;
- a strict “100% compatible” guarantee requires a Stream Server build whose FFmpeg command enforces the target profile;
- the integration rejects failed HLS conversion instead of silently reverting to direct playback.

## Source ranking

Format filters were removed. Automatic ranking now considers only:

```text
preferred resolution
→ seed count
→ file size
```

The existing bad-release and maximum-size controls remain available because they are not codec/container filters.

## Installation

Copy:

```text
custom_components/stremio_stream_bridge
```

to:

```text
/config/custom_components/stremio_stream_bridge
```

Then restart Home Assistant and add **Stremio Stream Bridge Encode** from **Settings → Devices & services**.

The internal Home Assistant domain remains `stremio_stream_bridge` for compatibility with the existing integration code and service names.

## Configuration

Set a Stream Server URL reachable by both Home Assistant and the Chromecast, for example:

```text
http://192.168.1.145:11470
```

Do not use `127.0.0.1` or `localhost`; the Chromecast opens the resulting media URL itself.

Typical provider defaults:

```text
Catalog and metadata:
https://v3-cinemeta.strem.io/manifest.json

Streams:
https://torrentio.strem.fun/manifest.json

Subtitles:
https://opensubtitles-v3.strem.io/manifest.json
```

## Services

The existing services remain available:

- `stremio_stream_bridge.search`
- `stremio_stream_bridge.resolve`
- `stremio_stream_bridge.play`
- `stremio_stream_bridge.play_url`
- `stremio_stream_bridge.refresh`

Example:

```yaml
action: stremio_stream_bridge.play
data:
  media_type: movie
  media_id: tt0133093
  media_player: media_player.tv_living
```

## Notes

- External subtitles are still converted to WebVTT and served through Home Assistant for Cast entities.
- Existing live HLS/DASH add-ons may not be accepted by Stream Server's torrent-oriented HLS route; failed encode attempts move to the next source.
- Transcoding 4K/HEVC sources in real time can be demanding. Selecting a 1080p source reduces CPU/GPU load even though the final output is capped at 1080p.
- Use only media, catalogs and providers you are authorized to access and reproduce.
