# 15 minutes. For whom?

An interactive map that shows how far you can walk in a given time, and how that changes with walking speed and with avoiding stairs. The same 15 minutes reach very different places for different people.

Try it: <https://martincantcode.github.io/15-minutes/> (currently covers Cologne).

Set a starting point by clicking the map, then adjust:

- **Time budget** (5 to 30 minutes)
- **Walking speed** (0.8 to 1.8 m/s; the range follows the meta-study by Giannoulaki & Christoforou)
- **Avoid stairs** (step-free)

Streets you can reach are coloured by walking time. Streets that would be reachable at 1.4 m/s with stairs allowed, but not with your settings, are shown in grey. The panel counts amenities (groceries, health, education, parks and playgrounds, cafés and restaurants), benches and stations within reach, and shows which stations OpenStreetMap tags as step-free. A "Copy link to this view" button stores your settings in the address.

## How it works

Everything runs in the browser. `export_city.py` downloads a walking network and points of interest from OpenStreetMap once and writes three small files to `data/<city>/`. The page loads them and runs the shortest-path search itself, so there is no server, no routing service and no quota. The repository includes an export for Cologne in `data/cologne/`.

## Run it on your computer

Open a terminal in this folder and start a small web server (opening `index.html` directly does not work, because the page has to load the data files):

```
python3 -m http.server 8000
```

Then open <http://localhost:8000>.

The background map comes from OpenFreeMap (free vector tiles, no key needed), drawn with MapLibre GL through the leaflet-maplibre-gl bridge. To use another OpenFreeMap style, change `BASEMAP_STYLE` near the top of the script in `index.html` (for example `liberty` or `bright` instead of `positron`). If the background map cannot be shown (no WebGL, or the tile service refuses requests), the page draws the street network itself as the basemap.

## Make it for another city

```
pip install osmnx
python export_city.py "Vienna, Austria" vienna --radius 8000
```

This writes `data/vienna/`. Then change `DATA_DIR` near the top of the script in `index.html` to `"data/vienna/"`. Large cities can overload the free OpenStreetMap servers, which is why `--radius` (in metres around the centre) exists. If a download keeps failing with "504", try a smaller radius or `--overpass` with another server.

## Limits

This is a prototype.

- Public transport travel, slope and street lighting are not included.
- Street, amenity and station data come from OpenStreetMap and are incomplete in places. Stations count as step-free only if OpenStreetMap tags them `wheelchair=yes`.
- Amenities are matched to the nearest street intersection, and parks and larger buildings count as single points, so counts near the edge of the reach are approximate.
- Streets that OpenStreetMap marks as having separately mapped sidewalks may be missing from the network where those sidewalks are not mapped.

## Data

Street, amenity and station data: © OpenStreetMap contributors, available under the Open Database Licence. See <https://www.openstreetmap.org/copyright>. The files in `data/` are derived from that data.

Basemap: © OpenFreeMap, © OpenMapTiles, data from OpenStreetMap.

## Licence

The code is released under the MIT licence (see `LICENSE`). The files in `data/` are derived from OpenStreetMap and remain under the Open Database Licence; the MIT licence does not cover them.

The `vendor/` folder holds unmodified copies of MapLibre GL JS 5.24.0 (BSD-3-Clause) and the leaflet-maplibre-gl bridge 0.1.4 (ISC), each with its own licence file; the MIT licence does not cover them either. Leaflet 1.9.4 (BSD-2-Clause) is loaded from cdnjs.

## Hosting your own copy

If you publish a copy on the web, you are its provider and are responsible for any legal notices that apply to you where you live (in Germany, for example, an imprint).
