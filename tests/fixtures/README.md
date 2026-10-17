# Test fixtures

Most are **hand-written** in the exact shape of the real responses (setlist.fm
REST API 1.0 JSON; schema.org JSON-LD as embedded by ticketing/venue pages).
The development environment couldn't reach those sites, so they are not
captured responses. When a real response breaks a parser, capture it here and
add a test.

- `setlistfm_artist_page1.json` — `/artist/{mbid}/setlists?p=1`, two setlists.
- `setlistfm_venue_night.json` — `/venue/{id}/setlists`, a headliner and an
  opener on the same night (opener's setlist filed under the headliner's tour).
- `event_page.html` — an event page with a schema.org MusicEvent for the
  Iguana Death Cult / Flat Worms show at Zebulon, 2026-09-17.
- `pollstar_autocomplete_zebulon.json`, `pollstar_events_zebulon.json` — Pollstar
  autocomplete and venue-events responses, in the shape documented in
  MusicSceneMap's `src/api/pollstar.ts`.
- `calendar_page.html` — a venue calendar with several events in an
  `@graph`, one cancelled, one with a single performer and a "w/" title.
- `gemini_generate_zebulon.json` — **captured** (2026-10-17) from a real
  search-grounded `gemini-3.8-flash` `generateContent` call for the Zebulon
  show; only the opaque `thoughtSignature` is truncated. Note it has no
  `groundingMetadata`, which really happens.
- `gemini_generate_grounded.json` — hand-written: the same answer shape with
  `groundingMetadata` (search queries and sources), a thought part, and one
  opener that the cited page doesn't mention.
- `gemini_models.json` — hand-written subset of a real `GET /v1beta/models`
  listing (model names as returned on 2026-10-17).
