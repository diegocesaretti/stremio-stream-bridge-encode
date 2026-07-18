# Chromecast 1st generation encode profile

`Stremio Stream Bridge Encode` never sends the original torrent media file to the player. It asks Stream Server for an HLS conversion and requires the following conservative output contract:

| Setting | Target |
|---|---|
| Container / delivery | HLS with MPEG-TS segments |
| Video | H.264 High Profile, level 4.1 or lower |
| Maximum picture | 1920×1080 at 30 fps |
| Maximum video bitrate | 8 Mbit/s |
| Pixel format | 8-bit 4:2:0 (`yuv420p`) |
| Audio | AAC-LC, stereo, 48 kHz, 192 kbit/s |

Google documents H.264 High Profile up to level 4.1 for Chromecast 1st and 2nd generation, with 1080p limited to 30 fps.

## Important Stream Server requirement

The integration adds these limits to the HLS request, but the currently published `perpetus/stream-server` HLS implementation does not consume every query parameter. Its FFmpeg defaults ultimately decide the real output profile.

For a strict compatibility guarantee, the Stream Server build running on the PC must enforce the contract above in its FFmpeg command. In particular, it must not advertise or encode H.264 level 5.1, retain 4K dimensions, produce more than 30 fps, or pass through multichannel/non-AAC audio.

Until the encoder itself enforces those limits, this integration is encode-first and substantially safer than direct playback, but “100% compatible” cannot be verified from Home Assistant alone.
