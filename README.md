# WarThunderRPC-Plus

Discord Rich Presence for War Thunder that says something worth reading.

War Thunder quietly serves telemetry over HTTP on `127.0.0.1:8111` on every machine. This reads it and mirrors it into your Discord status — the vehicle, the map, the mode, and for aircraft the live Mach number, airspeed and what you are doing with the aeroplane. It never writes to the game, never modifies game files and never injects anything. It is a read-only HTTP client plus, optionally, a screenshot.

```
┌───────────────────────────────────────────────────┐
│  Playing War Thunder                              │
│  Dogfighting · Air Domination on Kursk            │
│  F-4E PHANTOM · Mach 0.94 · 1120 km/h IAS         │
│  12:47 elapsed                                    │
└───────────────────────────────────────────────────┘
```

## What it shows

### Aircraft and helicopters

| Field | Source |
|---|---|
| Mach number, to two decimals | `/state` `M`, shown above Mach 0.30 |
| Indicated airspeed | `/state` `IAS, km/h` |
| Flight regime | derived, see below |
| Selected weapon, e.g. `AIM-120A` | HUD OCR, optional |

Helicopters report `army: "air"` exactly like aeroplanes and work the same way. An AH-64A cruises around Mach 0.05, which is why Mach is hidden below 0.30 — the figure says nothing down there and airspeed does.

### Ground vehicles

| Field | Source |
|---|---|
| Ready-rack rounds | `/indicators` `first_stage_ammo` |
| Crew remaining, when depleted | `crew_current` / `crew_total` |
| Knocked-out modules | breech, vertical and horizontal drive |
| Ammunition carried, e.g. `APFSDS + HEATFS` | the profile save |
| Round loaded, e.g. `APFSDS` | gunner-sight OCR, optional |

A tank in the original project showed nothing but its own name.

### The flight regime

Five states, from a rolling 20-second window:

| Regime | Hard manoeuvring | Hostile within 2.5 km |
|---|---|---|
| **Dogfighting** | yes | yes |
| **Maneuvering** | yes | no |
| **Engaged** | no | yes |
| **Supersonic** | — | Mach ≥ 1.0 |
| **Climbing** / **Diving** / **Cruising** | — | by vertical speed |

"Hard manoeuvring" means at least 35% of the window above 3G, or a mean heading-change rate of 8°/s or more while banked past 45°.

**Why the three-way split.** The instruments cannot tell a turning fight from aerobatics — both are sustained high-G, banked, fast-turning flight. All three cases were measured in one session: an 11.8G break with the nearest enemy 30 km away (a disengagement, not a fight), and 25 seconds spent between 0.5 and 2.6 km of an enemy fighter at Mach 1.4 while pulling barely over 1G (a firing pass, which "Supersonic" alone does not convey). What separates them is the distance to the nearest hostile, taken from `/map_obj.json`, using the closest approach anywhere in the window — a merge is brief, and two fighters passing inside a kilometre are three kilometres apart two seconds later.

Two honest caveats. Outside Arcade the minimap only shows enemies your team has spotted, so **no visible contact is not proof nobody is there**; that case reports *Maneuvering*, never a guessed *Dogfighting*. And the minimap carries no altitude, so the range is horizontal only.

In a Ground RB battle there are usually no enemy *aircraft* to measure against, so hard manoeuvring there reports as *Maneuvering* even under fire from SPAA.

## Install

```bash
git clone https://github.com/chawannua/WarThunderRPC-Plus.git
cd WarThunderRPC-Plus
pip install -r requirements.txt
python -m wtrpc
```

Start it whenever you like — before or after War Thunder, before or after Discord. It waits for both and reconnects on its own. `-v` turns on debug logging; a rolling log is kept beside the config file.

## Configuration

Written on first run to `%APPDATA%\WarThunderRPC-Plus\config.json`.

| Key | Default | Meaning |
|---|---|---|
| `client_id` | this project's app | Discord application ID — see below |
| `large_image` | `logo` | Asset key uploaded to that application, or an `https://` URL |
| `player_name` | `""` | Your exact in-game name. Required for the kill counter |
| `show_kills` | `false` | Append your confirmed kills for the match |
| `show_weapon` | `false` | Read the selected weapon or shell off the screen with OCR |
| `weapon_region` | `0,0,900,600` | Screen box `left,top,right,bottom` holding the aircraft weapon HUD |
| `show_map` | `true` | Identify and show the map name |
| `show_vehicle_image` | `true` | Vehicle thumbnail from the in-game encyclopedia |
| `show_flight_data` | `true` | Mach, airspeed, ammunition, crew |
| `dogfight_detection` | `true` | Enable the Dogfighting regime |
| `poll_interval` | `3.0` | Seconds between telemetry reads (clamped to 1.0–5.0) |
| `min_update_interval` | `15.0` | Seconds between Discord updates (floor 15.0, Discord's own limit) |
| `connect_timeout` / `read_timeout` | `0.5` / `1.5` | HTTP timeouts, clamped to sane values |

### Making Discord say "War Thunder"

Discord prints the **name of the application** the presence is published under, straight after *Playing*. No field in the payload changes it — not `large_text`, not `details`.

1. Create a **New Application** at [discord.com/developers/applications](https://discord.com/developers/applications). What you type as the name is what everyone sees after *Playing*.
2. Copy the **Application ID** into `client_id`.
3. Either upload an image under **Rich Presence → Art Assets** with the key `logo`, or point `large_image` at an `https://` URL instead.

An asset key only resolves against the application that owns it, so a new `client_id` with the old key shows a blank icon. The vehicle thumbnail is a URL and works either way.

### The kill counter

`/hudmsg` carries the kill feed but no identity at all — `sender`, `enemy` and `mode` were empty or constant across all 144 entries of a live match. Without your name the only way to find you is to match the vehicle model, and in a live test that locked onto a stranger in another M1A2 and reported his kills as the player's. So `player_name` must be set exactly, and the counter stays off rather than risk showing someone else's score.

### Reading the weapon off the screen

The game does not expose the selected weapon or shell anywhere in its API — see *Known limits*. With `show_weapon` enabled and [Tesseract](https://github.com/tesseract-ocr/tesseract) installed (`winget install tesseract-ocr.tesseract`, plus `pip install pytesseract`), the HUD can be read directly instead.

It works, and it is genuinely limited. Aircraft weapon names are drawn in the top-left block; shell names only in the gunner sight. Neither is on screen all the time, so the last good reading is kept rather than blanking the status. For ground vehicles the loadout from the profile save fills the gap, and in practice that is the reliable source while OCR is the bonus.

## Known limits

- **The selected weapon and shell are not in the API.** A moderator on the official forum [confirmed](https://forum.warthunder.com/t/weapon-and-ammunition-info-in-localhost/345655) that ammunition count and weapon state are absent from `/state` and `/indicators`, and advised against reading memory or modifying the client to get at them. Independently verified here: all seven endpoints exhausted, nineteen plausible undocumented paths probed and all 404, every tank indicator field diffed while shells were switched — only `first_stage_ammo` moved, and only when firing. The game's own `.clog` is a proprietary binary (magic `c0d2de0c`) matching no standard compression. The profile save holds the *loadout* but not the live selection: a diff across deliberate shell switching came back with zero changed lines.
- **Crew casualties are not reported.** `gunner_state` and `driver_state` looked like they meant "out of action", but an HSTV-L reported 1 for both while carrying a full crew of three. One pair of samples was not enough to establish the semantics, so the fields are unused rather than wrong.
- **Map names come from a lookup table** of perceptual hashes compiled in 2024 (see `NOTICE`). Maps added since show no name; everything else still works.
- **Mode labels fall back to a generic** on non-English clients rather than guessing wrong, because the objective text is localised.
- **Dogfight detection is a heuristic.** Aggressive aerobatics near an enemy read as a dogfight; a patient boom-and-zoom may not.

## Fixes carried over from the original

This began as a rewrite of [ValerieOSD/WarThunderRPC](https://github.com/ValerieOSD/WarThunderRPC), which had the right idea and several bugs that broke its headline features.

| Bug | What actually happened |
|---|---|
| **Operator precedence** | `A and B and C or in_match is False` parses as `(A and B and C) or (not in_match)`, so the first branch fired for *every* out-of-match state. Test Drive always displayed "Loading into a match", and the real Test Drive branches were unreachable dead code. |
| **Dead comparison** | `vehicleName == "DUMMY PLANE"` compared against the raw value `dummy_plane`. Never matched. |
| **No request timeouts** | A stalled port 8111 hung the process forever. |
| **Died on exit** | Closing the game called `exit()`. It now waits and reconnects, to the game *and* to Discord. |
| **Wrong timer** | The elapsed counter started at app launch, not at match start. |
| **Minimap thrash** | `map.img` was downloaded and written to the working directory every 3 seconds, even in the hangar. Now fetched only on entering a map and held in memory. |
| **English-only modes** | `startswith()` against English objective text meant a non-English client fell through every branch. |
| **Rate limiting** | Discord accepts roughly one update per 15 seconds and drops the rest. Updates now fire only on real change. |
| **Bare `except:`** | Swallowed `KeyboardInterrupt`. |
| **Dead thumbnail host** | Every thumbnail pointed at `encyclopedia.warthunder.com`, which **no longer resolves in DNS** — Gaijin retired it. Images now come from `static.encyclopedia.warthunder.com`. |

It also drops the `imagehash` dependency, which drags in `scipy`, `numpy` and `PyWavelets` — roughly 60 MB for one 12-line function. That function is now twelve lines of Pillow, verified bit-for-bit against `imagehash` in the test suite.

## Notes for anyone building on this

**Console encoding.** The presence strings contain a middle dot. On a Windows console running a non-Latin codepage — cp874 for Thai, cp932 for Japanese, cp936 for Chinese — printing one raises `UnicodeEncodeError` and takes the process with it. Discord itself is UTF-8 and never had a problem; only logging did. `wtrpc` reconfigures stdout and stderr at startup, and anything you write alongside it should too. This bites at runtime on the user's machine and never in CI, which is exactly why it is easy to ship.

**`/state` is aviation only.** A ground vehicle answers it with `{"valid": false}` and nothing else. The field set in `/indicators` also changes completely by vehicle: 79 fields in an F-16C, 23 in an M1A2, 51 in an AH-64A. Treat every key as optional.

**The game lies for one frame after a respawn.** The first poll on a newly spawned vehicle carries the *previous* one's state — a pristine ADATS reported a destroyed breech and both drives out, precisely the wreck of the M1A2 that had just died. Ground readings are ignored until the same vehicle has been seen twice.

**Ask the match, not the vehicle.** Spawning a helicopter in a Ground RB battle does not make it an air battle, and during the load screen the reported army is simply whatever was selected in the hangar. The battle type comes from the minimap instead: ground battles put tank respawn points on it and air battles never do.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

The design keeps the game logic pure. `presence_builder.py`, `modes.py`, `flight.py`, `ground.py`, `contacts.py`, `naming.py` and `loadout.py` do no I/O, take plain dataclasses from `models.py`, and are tested with War Thunder closed. Only `wtclient.py`, `presence.py` and `weapon_ocr.py` touch the network, Discord or the screen.

Fixtures under `tests/fixtures/` are real captures — live telemetry payloads, two screenshots of the aircraft weapon HUD and two of the gunner sight — rather than invented data, because inventing them is how the colour thresholds and the field semantics went wrong the first time.

## Credits

Original concept, and the idea of surfacing this over Rich Presence: [ValerieOSD/WarThunderRPC](https://github.com/ValerieOSD/WarThunderRPC).
Map hash table: [PowerBroker2/WarThunder](https://github.com/PowerBroker2/WarThunder).
API reference: [lucasvmx/WarThunder-localhost-documentation](https://github.com/lucasvmx/WarThunder-localhost-documentation).

See [NOTICE](NOTICE) for attribution details and a licensing caveat about the map table.

## License

MIT — see [LICENSE](LICENSE).
