# 15 minutes. For whom?

An interactive map that shows how far you can walk in a given time, and how that changes with walking speed and with avoiding stairs. The same 15 minutes reach very different places for different people.

Try it: <https://martincantcode.github.io/15-minutes/> (Cologne; more cities can be added, see below).

Set a starting point by clicking the map, then adjust:

- **Time budget** (5 to 30 minutes)
- **Walking speed** (0.8 to 1.8 m/s; the range follows the meta-study by Giannoulaki & Christoforou)
- **Avoid stairs** (step-free)
- **Account for slope** (only in cities that have height data): from 0 (hills make no difference) through the standard setting to a stricter one, and optionally **avoid slopes steeper than** a limit you choose (3 to 15 %). A "Show steep streets" option marks the streets above that limit.

Streets you can reach are coloured by walking time. Streets that would be reachable at 1.4 m/s with stairs allowed, but not with your settings, are shown in grey. The panel counts amenities (groceries, health, education, parks and playgrounds, cafés and restaurants), benches and stations within reach, and shows which stations OpenStreetMap tags as step-free. A "Show stairs" option marks the flights of steps next to the area you can reach in red, which shows where avoiding stairs cuts a route; steps that OpenStreetMap tags with a wheelchair ramp (`ramp:wheelchair=yes`) are shown in orange and count as passable. A "Copy link to this view" button stores your settings, the city and the position in the address.

## How it works

Everything runs in the browser. `export_city.py` downloads a walking network and points of interest from OpenStreetMap once and writes them to `data/<city>/`. The page loads them and runs the shortest-path search itself, so there is no server, no routing service and no quota. The repository includes an export for Cologne in `data/cologne/`.

A city can be stored in two ways. Small and medium cities are one file that the page loads completely. Large areas are cut into square tiles (`--tile-km`): the page then downloads only the tiles around the point you click, as many as the time budget and walking speed require, and keeps them in memory. The tiles overlap where a street crosses a border and are merged without duplicates, so the results are the same as with one file. `data/cities.json` lists the cities in the menu on the page.

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

This writes `data/vienna/` and adds the city to `data/cities.json`, which fills the menu on the page (the menu appears once there are two cities). Open it with `#city=vienna` at the end of the address, or choose it in the menu.

For a big area, cut it into tiles so that visitors only download what they use:

```
python export_city.py "Shibuya Station, Tokyo, Japan" tokyo --name "Tokyo (Shibuya)" --radius 8000 --tile-km 2.5
```

The area is a square around the place, `--radius` metres from its centre to each side. If the place name does not land where you want the centre, give the point yourself with `--center LAT,LON` (for example `--center 19.35,-99.14`); the place name is then only a label. Tiling makes the page light, but the export still downloads and processes the whole area in one go, so grow it in steps (for example 8000, then 12000) and watch the time and memory. The script prints the number of tiles and their sizes at the end; if the largest tile is above about 2 MB, run it again with a smaller `--tile-km` (the downloaded data is cached, so a repeat run with the same place and radius is quick). `--name` is the text shown in the menu. Large downloads can overload the free OpenStreetMap servers, which is why `--radius` (in metres around the centre) exists. If a download keeps failing with "504", try a smaller radius.

To export the whole administrative area of a place instead of a square, leave out `--radius`:

```
python export_city.py "Greater London, England" london --name London --center 51.508,-0.128 --tile-km 5
```

The script looks up the boundary of the named place in OpenStreetMap and prints what it found (name, area, number of boundary points) before it downloads anything, so check that it is the area you meant and press Ctrl+C if not. Without `--radius`, `--center` only chooses where the page starts. If the name finds the wrong area, give the boundary's OpenStreetMap relation with `--osm-id R1234567` (the place name is then only a label). A very detailed boundary is simplified by less than about 30 m before it is sent to the server. The amenities of a whole place are downloaded box by box, starting with boxes about 8 km wide (`--chunk-km`, 0 = one request). A box that is too heavy for the server, as dense city centres can be, is split into four and its parts are asked for again, so sparse areas go quickly and dense ones get small boxes. Each request asks only for tags and one centre point per object, which keeps the answers small. A request that a server refuses or throttles (429) is repeated after a pause, switching to the next of three public Overpass servers (`--overpass` takes one or more addresses, separated by commas, if you prefer your own). The amenities are downloaded before the streets, because they are the part the free servers refuse most often, so a problem shows up within minutes. Answers are saved in `cache/amenities`, so after a failure a repeated run carries on where it stopped. If every server refuses, wait a while (public servers may block an address for some time after many requests) and run the same command again. 

For big cities there is a faster and more dependable route: download the area as a file from [Geofabrik](https://download.geofabrik.de) (for example `greater-london-latest.osm.pbf`, about 123 MB) and read the amenities from it with `--pbf`, without asking any server:

```
pip install osmium
python export_city.py "Greater London, England" london --name London --center 51.508,-0.128 --tile-km 5 --pbf greater-london-latest.osm.pbf
```

The categories, wheelchair tags and rules are the same as with the servers. The streets are still downloaded as before (and cached by osmnx); only the amenities come from the file.

The public Overpass servers are sometimes overloaded or refuse connections. With `--pbf-streets` (together with `--pbf`) the walking network is read from the same file, so no server is needed at all:

```
python export_city.py "Innsbruck, Austria" innsbruck --center 47.2665,11.3945 --radius 20000 --tile-km 5 --pbf austria-latest.osm.pbf --pbf-streets
```

The rules for which ways count as walkable are the same as OSMnx applies for its walking network, so the result should match the server route; reading a country-sized file takes a few minutes. This route has been tested on a small synthetic extract, not compared street by street with the server route.

With either route, a large park or building is placed at the centre of its bounding box. By default only the largest connected piece of the street network is kept, so that a click cannot land in a small disconnected fragment; `--keep-all` keeps every piece, and with the page's "Show all streets in the data" option the disconnected streets then show up in grey, which helps to find gaps in the data. Streets outside the boundary are not part of the data, so a reach that would cross the boundary stops there.

Copy the new `data/<city>/` folder and the updated `data/cities.json` into the repository. A tiled city is many small files (one per tile). The GitHub website accepts at most 100 files per commit, so a larger city needs several uploads, or use GitHub Desktop or Git.

## Terrain

A city has terrain if `data/<city>/elev.bin` exists. It is made from a digital elevation model (DEM) by `add_elevation.py`, which reads the junctions of a city that is already exported, so the street data is not downloaded again:

```
pip install numpy rasterio pyproj requests
python add_elevation.py luzern --swissalti3d          # Switzerland: downloads swisstopo's 2 m model automatically
python add_elevation.py cologne --nrw-dgm1            # North Rhine-Westphalia: downloads Geobasis NRW's 1 m model for the 1 km squares that contain streets (about 2 MB each)
python add_elevation.py cologne --dem path/to/dem.tif  # any DEM as GeoTIFF file(s), a folder of them, or a .zip
```

The slope of a street is the height difference between its two end junctions divided by its length (streets shorter than 20 m count as 20 m, so that a small height error does not look like a cliff). Only junctions are looked up in the DEM. Bridges and tunnels are therefore not a problem, because a bare-earth model shows a bridge as a valley, but its ends are on the ground. The price is that a street that climbs and falls between two junctions looks flatter than it is.

Speed on a slope follows Tobler's hiking function (W = 6 exp(-3.5 |S + 0.05|) km/h, with S the slope as rise over run), expressed as a share of the speed on the flat: a 5 % climb makes you 16 % slower, a 10 % climb 30 % slower, and a gentle downhill of 5 % up to 19 % faster. The slider scales the penalty for climbing: at 0 it is off, at 1 it is Tobler as published, and at 2 it is twice as strict, which is close to the rule of thumb "every metre climbed counts as 10 m of walking" (30 % slower at 5 %, 50 % at 10 %). The downhill bonus is never made stronger than Tobler's. The grey reference area ignores slopes.

The limit "avoid slopes steeper than X %" removes streets whose slope is above X % in either direction. 6 % is the general limit for an accessible pedestrian route in Spain's Orden TMA/851/2021 (the Madrid version uses it as its default), but the filter is not a compliance check: such rules allow steeper ramps under some conditions and measure slope along the path, which this tool cannot.

Tobler's function describes walking on paths, not wheelchair use. Slope plus step-free routing is not a wheelchair route.

Heights for Switzerland: swissALTI3D © swisstopo (open government data). Heights for Cologne and surroundings: Digitales Geländemodell 1 m, Geobasis NRW, [Datenlizenz Deutschland – Zero – Version 2.0](https://www.govdata.de/dl-de/zero-2-0) (use without restrictions or conditions; credit given here anyway). Heights for Innsbruck: Digitales Geländemodell Tirol (5 m), © Land Tirol – data.gv.at, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Terrain in this tool was inspired by the Madrid version by [pablolamiquiz](https://github.com/pablolamiquiz/15--minutos-madrid), a fork of this project, which uses the same hiking function, a slope limit and a steep-street layer. The code here was written independently for this project's data format.

## Limits

This is a prototype.

- Public transport travel and street lighting are not included. Slope is included only for cities with an `elev.bin` (see Terrain).
- Street, amenity and station data come from OpenStreetMap and are incomplete in places. Stations count as step-free only if OpenStreetMap tags them `wheelchair=yes`.
- Amenities are matched to the nearest street intersection, and parks and larger buildings count as single points, so counts near the edge of the reach are approximate.
- With `--pbf-streets`, cycleways tagged `foot=yes`, `foot=designated` or `foot=permissive` count as walkable (shared foot and cycle paths, which is how many bridge sidewalks are mapped). The Overpass route leaves all cycleways out. `why_missing.py` shows, for any point, which nearby ways are kept or left out and why.
- Streets that OpenStreetMap marks as having separately mapped sidewalks may be missing from the network where those sidewalks are not mapped.
- Escalators are mapped in OpenStreetMap as steps, so avoiding stairs also avoids them; lifts are not modelled.
- Only `ramp:wheelchair=yes` makes a flight of steps passable. `ramp=yes` alone can mean a stroller or bicycle ramp, and a ramp mapped as its own way is part of the network anyway. Ramps that are not tagged are not known to the tool. The export prints how many steps it found and how many carry the ramp tag.

## Data

Street, amenity and station data: © OpenStreetMap contributors, available under the Open Database Licence. See <https://www.openstreetmap.org/copyright>. The files in `data/` are derived from that data.

Basemap: © OpenFreeMap, © OpenMapTiles, data from OpenStreetMap.

## Licence

The code is released under the MIT licence (see `LICENSE`). The files in `data/` are derived from OpenStreetMap and remain under the Open Database Licence; the MIT licence does not cover them.

The `vendor/` folder holds unmodified copies of MapLibre GL JS 5.24.0 (BSD-3-Clause) and the leaflet-maplibre-gl bridge 0.1.4 (ISC), each with its own licence file; the MIT licence does not cover them either. Leaflet 1.9.4 (BSD-2-Clause) is loaded from cdnjs.

## Hosting your own copy

If you publish a copy on the web, you are its provider and are responsible for any legal notices that apply to you where you live (in Germany, for example, an imprint).
