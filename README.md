# WarThunderRPC-Plus

Discord Rich Presence for War Thunder — with **live Mach number, indicated airspeed, and dogfight detection**.

War Thunder quietly serves telemetry over HTTP on `127.0.0.1:8111` on every machine. This reads it and mirrors it into your Discord status. It never writes to the game, never touches game files, and never injects anything — it is a read-only HTTP client, so it cannot get you banned.

```
┌─────────────────────────────────────────────┐
│  War Thunder                                │
│  Dogfighting · Air Domination on Kursk      │
│  F-4E PHANTOM · Mach 0.94 · 1120 km/h IAS   │
│  12:47 elapsed                              │
└─────────────────────────────────────────────┘
```

## What makes this different

This started as a rewrite of [ValerieOSD/WarThunderRPC](https://github.com/ValerieOSD/WarThunderRPC), which had the right idea but a handful of bugs that broke its headline features. Everything below is either a fix or something new.

### Flight telemetry (new)

For aircraft, the presence carries live data straight from the game's `/state` endpoint:

- **Mach number** to two decimal places
- **Indicated airspeed** in km/h
- **Flight regime**, inferred from a rolling 20-second window of telemetry:

| Regime | How it is detected |
|---|---|
| **Dogfighting** | Hard manoeuvring **and** a hostile aircraft within 2.5 km |
| **Maneuvering** | The same hard manoeuvring with nobody close |
| **Supersonic** | Mach ≥ 1.0 |
| **Climbing** | Mean vertical speed ≥ 15 m/s |
| **Diving** | Mean vertical speed ≤ −25 m/s |
| **Cruising** | Anything else |

"Hard manoeuvring" means ≥35% of the window above 3G, or a mean heading change rate ≥8°/s while banked past 45°.

**Why the split matters.** The instruments cannot tell a turning fight from aerobatics — both are sustained high-G, banked, fast-turning flight. Measured in a live match: a 9.4G break with the nearest enemy 5.8 km away and opening reads *identically* to a knife fight. It is a disengagement, and calling it a dogfight is simply wrong. So the distinction comes from `/map_obj.json`, which carries the player's and every marked aircraft's position; the closest approach anywhere in the window decides it, because a merge is brief and two fighters passing inside a kilometre are three kilometres apart two seconds later.

Two honest caveats. The minimap only shows enemies your team has spotted outside Arcade, so **no visible contact is not proof nobody is there** — that case reports *Maneuvering*, never a guessed *Dogfighting*. And the minimap has no altitude, so the range is horizontal only.

All of it works in **Test Flight** as well as in matches. `/state` serves full telemetry there, and test flight is how most people first try an app like this, so hiding the headline feature until a real match made it look broken.

### Fixes carried over from the original

| Bug | What actually happened |
|---|---|
| **Operator precedence** | `A and B and C or in_match is False` parses as `(A and B and C) or (not in_match)`, so the first branch fired for *every* out-of-match state. Test Drive always displayed "Loading into a match", and the real Test Drive branches were unreachable dead code. |
| **Dead comparison** | `vehicleName == "DUMMY PLANE"` compared against the raw value `dummy_plane`; the underscore was stripped into a different variable. Never matched. |
| **No request timeouts** | A stalled port 8111 hung the process forever. Every request now has a connect and read timeout. |
| **Died on exit** | Closing the game called `exit()`. It now waits and reconnects, to the game *and* to Discord. |
| **Wrong timer** | The elapsed counter started at app launch, not at match start. It now resets on every state change. |
| **Minimap thrash** | `map.img` was downloaded and written to the working directory every 3 seconds, even in the hangar. Now fetched only on entering a map, held in memory, never written to disk. |
| **English-only** | Match modes were matched with `startswith()` against English objective text, so a non-English client fell through every branch. Unmatched text now degrades to a safe generic label instead of a wrong one. |
| **Rate limiting** | Discord accepts roughly one presence update per 15 seconds and drops the rest. Updates now fire only on actual change, and a changed payload is held rather than dropped. |
| **Bare `except:`** | Swallowed `KeyboardInterrupt`. All handlers are now specific. |
| **Dead thumbnail host** | Every vehicle thumbnail pointed at `encyclopedia.warthunder.com`, which **no longer resolves in DNS at all** — Gaijin retired it. Images now come from `static.encyclopedia.warthunder.com`, verified returning real PNGs. |

### Lighter dependencies

The original pulled in `imagehash`, which drags `scipy`, `numpy` and `PyWavelets` along — roughly 60 MB for one 12-line function. That function is now implemented directly on top of Pillow and verified bit-for-bit against `imagehash` in the test suite. Runtime dependencies are `requests`, `pypresence` and `pillow`.

## Install

### From source

```bash
git clone https://github.com/chawannua/WarThunderRPC-Plus.git
cd WarThunderRPC-Plus
pip install -r requirements.txt
python -m wtrpc
```

Start it any time — before or after War Thunder, before or after Discord. It waits for both and reconnects on its own.

## Configuration

A config file is written on first run to `%APPDATA%\WarThunderRPC-Plus\config.json`:

| Key | Default | Meaning |
|---|---|---|
| `poll_interval` | `3.0` | Seconds between telemetry reads (floor 1.0) |
| `min_update_interval` | `15.0` | Seconds between Discord updates (floor 15.0, Discord's own limit) |
| `show_map` | `true` | Identify and display the map name |
| `show_vehicle_image` | `true` | Show the vehicle thumbnail from the in-game encyclopedia |
| `show_flight_data` | `true` | Show Mach and airspeed for aircraft |
| `dogfight_detection` | `true` | Enable the Dogfighting regime |
| `client_id` | — | Discord application ID |

Run with `-v` for debug logging. A rolling log is kept next to the config file.

### Making Discord say "War Thunder"

Discord shows the **name of the application** the presence is published under, straight after the word *Playing*. No field in the payload changes it — not `large_text`, not `details`. Out of the box this project inherits the upstream application ID, so it reads as that application's name instead.

To own it, register your own application — it takes about three minutes:

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and create a **New Application**. What you type as the name is exactly what everyone will see after *Playing*, so name it **War Thunder**.
2. Copy the **Application ID** from the General Information page.
3. Give it an image, either way round:
   - **Rich Presence → Art Assets** → upload an image with the key `logo`, or
   - skip that and point `large_image` at a plain `https://` URL instead.
4. Put both into `%APPDATA%\WarThunderRPC-Plus\config.json`:

```json
{
  "client_id": "your-application-id-here",
  "large_image": "logo"
}
```

An asset key only resolves against the application that owns it, so a new `client_id` needs either its own uploaded `logo` asset or a URL in `large_image` — otherwise the large icon comes up blank. The vehicle thumbnail is a URL and keeps working either way.

## Known limits

- **The selected weapon cannot be shown.** War Thunder does not expose it. The API has exactly two weapon-related fields, `weapon2` and `weapon3` in `/indicators`, and the official documentation leaves both descriptions blank. Measured against a live F-4S over 468 samples covering three minutes of actively cycling through every weapon, both stayed at `0.0` and never moved once. There is no other candidate field across any of the eight endpoints.
- **Helicopters report `army: "air"`**, the same as aeroplanes, so they are recognised but described as aircraft. Mach is meaningless for a rotorcraft and is suppressed below Mach 0.05 anyway.
- **Map names come from a lookup table** of perceptual hashes compiled in 2024 (see `NOTICE`). Maps added since then show no name — everything else still works.
- **Ground and naval vehicles have no flight data**, because `/state` only reports aircraft telemetry. Naval presence shows the vehicle and mode only.
- **Mode labels fall back to a generic** ("Air Battle") on non-English clients rather than guessing wrong.
- **Dogfight detection is a heuristic.** Aggressive aerobatics read as a dogfight; a patient boom-and-zoom pass may not.
- **The Discord application ID is inherited** from the original project, so the presence renders under that application's name rather than "War Thunder". See below to fix it.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

The design keeps all game logic pure: `wtrpc/presence_builder.py`, `wtrpc/modes.py`, `wtrpc/flight.py` and `wtrpc/naming.py` do no I/O, take plain dataclasses from `wtrpc/models.py`, and are tested with War Thunder closed. Only `wtrpc/wtclient.py` and `wtrpc/presence.py` touch the network.

## Credits

Original concept, Discord application and logo: [ValerieOSD/WarThunderRPC](https://github.com/ValerieOSD/WarThunderRPC).
Map hash table: [PowerBroker2/WarThunder](https://github.com/PowerBroker2/WarThunder).
API reference: [lucasvmx/WarThunder-localhost-documentation](https://github.com/lucasvmx/WarThunder-localhost-documentation).

See [NOTICE](NOTICE) for attribution details and a licensing caveat about the map table.

## License

MIT — see [LICENSE](LICENSE).
