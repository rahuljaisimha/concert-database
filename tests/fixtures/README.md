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
- `pollstar_city_page.json` — one page of Pollstar's events in a city (Berlin),
  as MusicSceneMap's pollstar-city function stores it: two venues (one with a
  mailing address listed before its physical one, one listed again without
  addresses) and an event without a venue. Hand-written in the shape of a real
  response seen on 2026-10-29, with the fields we don't read trimmed.
- `calendar_page.html` — a venue calendar with several events in an
  `@graph`, one cancelled, one with a single performer and a "w/" title.
- `mistletone_ty_segall.html` — **captured** (2026-10-19): the title and tour-dates
  block of the promoter's Ty Segall page (https://mistletone.net/news/ty-segall/),
  unchanged; the rest of the page (navigation, promo text) is cut.
- `searxng_ty_tour.json`, `searxng_ty_venues.json` — **captured** (2026-10-19)
  SearXNG JSON answers for the Ty Segall tour query and venue-names query, cut to
  the top 10 results.
