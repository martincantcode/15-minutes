#!/usr/bin/env python3
"""Export a walking network and points of interest from OpenStreetMap for the
"15 minutes. For whom?" page.

Usage:
    pip install osmnx
    python export_city.py "Köln, Germany" cologne
    python export_city.py "Köln, Germany" cologne --radius 6000     # smaller area, lighter download
    python export_city.py "Köln, Germany" cologne --overpass https://overpass.private.coffee/api
    python export_city.py "Shibuya Station, Tokyo, Japan" tokyo --name "Tokyo (Shibuya)" --radius 8000 --tile-km 2.5
    python export_city.py "Mexico City" cdmx --name "Ciudad de México" --center 19.35,-99.14 --radius 15000 --tile-km 3.5
    python export_city.py "Greater London, England" london --name London --center 51.508,-0.128 --tile-km 5

Without --radius the whole boundary of the named place is exported (not a square), and --center then only
chooses where the page starts.

With --pbf FILE the amenities come from a downloaded extract (for example greater-london-latest.osm.pbf from
download.geofabrik.de) instead of an Overpass server, which is much faster for big cities
(needs: pip install osmium).

With --pbf-streets as well, the walking network also comes from the extract and no Overpass server is used at all.

Output, in data/<slug>/:
    graph.bin   street network (binary, read directly by the page)      } one city, loaded all at once
    pois.json   amenities, benches, stations                            }
    t/*.bin     the same data cut into square tiles (with --tile-km)    } big areas: the page loads only the
                                                                        } tiles around the point you click
    meta.json   name, bounding box, start point (and the tile grid)
and data/cities.json, the list that fills the city menu on the page.

The page is served from the same folder, so put index.html next to data/.
"""
import argparse
import hashlib
import json
import math
import re
import struct
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

CATS = ["Groceries", "Health", "Education", "Parks & play", "Cafés & eating", "Benches", "Stations"]
GROCERY = {"supermarket", "convenience", "greengrocer", "bakery", "butcher"}
HEALTH = {"pharmacy", "doctors", "clinic", "hospital", "dentist"}
EDUCATION = {"school", "kindergarten", "library"}
PARKS = {"park", "playground", "garden"}
EATING = {"cafe", "restaurant", "pub", "bar"}
WHEELCHAIR = {"yes": 1, "limited": 2, "no": 3}   # 0 = not tagged
_ORIGINAL_GETADDRINFO = socket.getaddrinfo   # osmnx replaces this to pin Overpass to one machine; see overpass_amenities
# public Overpass servers, tried in this order when one refuses or drops the connection
OVERPASS_SERVERS = ["https://overpass-api.de/api", "https://overpass.private.coffee/api", "https://overpass.kumi.systems/api"]


def categorize(tags):
    """Return (category index, wheelchair code) or None. `tags` is a plain dict of strings."""
    if tags.get("railway") in ("station", "tram_stop"):
        return 6, WHEELCHAIR.get(tags.get("wheelchair"), 0)
    amenity, shop, leisure = tags.get("amenity"), tags.get("shop"), tags.get("leisure")
    if amenity == "bench":
        return 5, 0
    if shop in GROCERY:
        return 0, 0
    if amenity in HEALTH:
        return 1, 0
    if amenity in EDUCATION:
        return 2, 0
    if leisure in PARKS:
        return 3, 0
    if amenity in EATING:
        return 4, 0
    return None


def is_steps(highway):
    values = highway if isinstance(highway, (list, tuple)) else [highway]
    return "steps" in values


def step_kind(data):
    """0 = an ordinary way, 1 = steps that stop someone who cannot use stairs, 2 = steps that come with a
    wheelchair ramp (OpenStreetMap: ramp:wheelchair=yes), which that person can use instead.
    Other ramp tags do not count: ramp=yes alone can mean a stroller or bicycle ramp, and ramp=separate means
    the ramp is its own way, which is already part of the network."""
    if not is_steps(data.get("highway")):
        return 0
    ramp = data.get("ramp:wheelchair")
    values = ramp if isinstance(ramp, (list, tuple)) else [ramp]
    return 2 if all(v == "yes" for v in values) else 1


def count_vertices(geom):
    """Number of boundary points of a (multi)polygon."""
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords) + sum(len(r.coords) for r in geom.interiors)
    if geom.geom_type == "MultiPolygon":
        return sum(count_vertices(g) for g in geom.geoms)
    return 0


def haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(a))


def e5(value):
    return int(round(value * 1e5))


def build_graph_arrays(G, simplify_tol_deg=3e-5):
    """Turn a networkx (OSMnx-style) graph into the arrays that make up the binary format.

    Returns a dict: lonlat (int32, degrees * 1e5), eu / ev (edge end nodes, uint32), lf (length in
    decimetres << 2 | flags, uint32), shape_start (uint32, edges + 1) and shape_arr (int32 lon/lat pairs).
    Flag bit 0 = a flight of steps, bit 1 = a flight of steps with a wheelchair ramp (see step_kind).
    Shape points are the bends between the two end nodes.
    """
    nodes = list(G.nodes)
    index = {n: i for i, n in enumerate(nodes)}
    lonlat = np.array([(e5(G.nodes[n]["x"]), e5(G.nodes[n]["y"])) for n in nodes], dtype=np.int32).reshape(-1, 2)

    best = {}   # (low node, high node, kind) -> (length in m, interior points low -> high)
    for u, v, _key, data in G.edges(keys=True, data=True):
        a, b = index[u], index[v]
        if a == b:
            continue
        kind = step_kind(data)
        length = data.get("length")
        if length is None:
            length = haversine_m(G.nodes[u]["x"], G.nodes[u]["y"], G.nodes[v]["x"], G.nodes[v]["y"])
        interior = []
        geom = data.get("geometry")
        if geom is not None:
            coords = list(geom.simplify(simplify_tol_deg, preserve_topology=False).coords)
            interior = [(e5(x), e5(y)) for x, y in coords[1:-1]]
            if a > b:
                interior.reverse()
        key = (min(a, b), max(a, b), kind)
        if key not in best or length < best[key][0]:
            best[key] = (float(length), interior)

    keys = sorted(best)
    n_edges = len(keys)
    eu = np.zeros(n_edges, dtype="<u4")
    ev = np.zeros(n_edges, dtype="<u4")
    lf = np.zeros(n_edges, dtype="<u4")
    shape_start = np.zeros(n_edges + 1, dtype="<u4")
    shape_pts = []
    for i, key in enumerate(keys):
        a, b, kind = key
        length, interior = best[key]
        eu[i], ev[i] = a, b
        lf[i] = (min(int(round(length * 10)), (1 << 30) - 1) << 2) | kind
        shape_pts.extend(interior)
        shape_start[i + 1] = len(shape_pts)
    shape_arr = np.array(shape_pts, dtype="<i4").reshape(-1, 2)
    return {"lonlat": lonlat, "eu": eu, "ev": ev, "lf": lf, "shape_start": shape_start, "shape_arr": shape_arr}


def graph_summary(a):
    lonlat = a["lonlat"]
    flags = a["lf"] & 3
    return {"nodes": len(lonlat), "edges": len(a["eu"]), "shape_points": len(a["shape_arr"]),
            "steps": int((flags == 1).sum()), "steps_with_wheelchair_ramp": int((flags == 2).sum()),
            "bbox": [float(lonlat[:, 0].min() / 1e5), float(lonlat[:, 1].min() / 1e5),
                     float(lonlat[:, 0].max() / 1e5), float(lonlat[:, 1].max() / 1e5)]}


def write_graph_bin(a, out_path):
    """Write the whole network as one binary file (version 1).

    Layout, little endian: 4 x uint32 header (version, nodes, edges, shape points), then
    node lon/lat (int32), edge from / to / (length in decimetres << 2 | flags) (uint32 each),
    shape start offsets (uint32, edges + 1) and shape points (int32 lon/lat pairs).
    """
    with open(out_path, "wb") as f:
        f.write(struct.pack("<4I", 1, len(a["lonlat"]), len(a["eu"]), len(a["shape_arr"])))
        for arr in (a["lonlat"].astype("<i4"), a["eu"], a["ev"], a["lf"], a["shape_start"], a["shape_arr"]):
            f.write(arr.tobytes())


def export_graph(G, out_path, simplify_tol_deg=3e-5):
    """Write a networkx (OSMnx-style) graph as one compact binary file and return a summary."""
    a = build_graph_arrays(G, simplify_tol_deg)
    write_graph_bin(a, out_path)
    return graph_summary(a)


def write_tiles(a, pts, out_dir, tile_km=2.5):
    """Cut the network and the points of interest into square tiles of about tile_km kilometres.

    Every edge is stored in the tile of each of its two end nodes (so an edge crossing a tile border is in
    both), together with the end nodes it needs. Each node keeps its number from the whole network, which
    lets the page merge tiles and drop duplicates. Points of interest go into the tile they lie in.
    One file per tile, t/<x>_<y>.bin, little endian: 5 x uint32 header (version 2, nodes, edges, shape
    points, bytes of JSON), then node ids (uint32), node lon/lat (int32), edge from / to (uint32, positions in
    this tile's node list), edge length+flags (uint32), shape start offsets, shape points, and finally the
    points of interest of this tile as JSON text: [[lon, lat, category, wheelchair], ...].
    Returns the tile grid description for meta.json.
    """
    out_dir = Path(out_dir)
    (out_dir / "t").mkdir(parents=True, exist_ok=True)
    for old_tile in (out_dir / "t").glob("*.bin"):   # tiles from an earlier run belong to a different grid
        old_tile.unlink()
    lonlat, eu, ev, lf = a["lonlat"], a["eu"], a["ev"], a["lf"]
    shape_start, shape_arr = a["shape_start"], a["shape_arr"]
    lon, lat = lonlat[:, 0] / 1e5, lonlat[:, 1] / 1e5
    pad = 0.01   # degrees (about 1 km) of margin: amenities just beyond the last street still get a tile
    lon0, lat0 = float(lon.min()) - pad, float(lat.min()) - pad
    dlat = tile_km * 1000 / 110540.0
    dlon = tile_km * 1000 / (111320.0 * math.cos(math.radians((float(lat.min()) + float(lat.max())) / 2)))
    tx = np.floor((lon - lon0) / dlon).astype(np.int64)
    ty = np.floor((lat - lat0) / dlat).astype(np.int64)
    nx = int(math.floor((float(lon.max()) + pad - lon0) / dlon)) + 1
    node_tile = ty * nx + tx

    tu, tv = node_tile[eu], node_tile[ev]
    cross = tv != tu
    edge_ids = np.arange(len(eu))
    pair_tile = np.concatenate([tu, tv[cross]])
    pair_edge = np.concatenate([edge_ids, edge_ids[cross]])
    order = np.argsort(pair_tile, kind="stable")
    pair_tile, pair_edge = pair_tile[order], pair_edge[order]
    cuts = np.flatnonzero(np.diff(pair_tile)) + 1
    tile_ids = pair_tile[np.concatenate([[0], cuts]).astype(np.int64)] if len(pair_tile) else []
    groups = np.split(pair_edge, cuts)

    poi_by_tile = {}
    for p in pts:
        ix, iy = int(math.floor((p[0] - lon0) / dlon)), int(math.floor((p[1] - lat0) / dlat))
        if 0 <= ix < nx and iy >= 0:
            poi_by_tile.setdefault(iy * nx + ix, []).append(p)

    def write_tile(tid, ids, leu, lev, lf_e, lstart, sp):
        key = "%d_%d" % (tid % nx, tid // nx)
        poi_json = json.dumps(poi_by_tile.get(tid, []), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with open(out_dir / "t" / (key + ".bin"), "wb") as f:
            f.write(struct.pack("<5I", 2, len(ids), len(leu), len(sp), len(poi_json)))
            for arr in (ids.astype("<u4"), lonlat[ids].astype("<i4"), leu, lev, lf_e.astype("<u4"), lstart, sp.astype("<i4")):
                f.write(arr.tobytes())
            f.write(poi_json)
        return key

    keys, written = [], set()
    for tid, edges in zip(tile_ids, groups):
        tid = int(tid)
        ids = np.unique(np.concatenate([eu[edges], ev[edges]]))
        leu = np.searchsorted(ids, eu[edges]).astype("<u4")
        lev = np.searchsorted(ids, ev[edges]).astype("<u4")
        starts = shape_start[edges].astype(np.int64)
        counts = shape_start[edges + 1].astype(np.int64) - starts
        lstart = np.zeros(len(edges) + 1, dtype="<u4")
        lstart[1:] = np.cumsum(counts)
        total = int(counts.sum())
        if total:
            idx = np.arange(total) + np.repeat(starts - lstart[:-1].astype(np.int64), counts)
            sp = shape_arr[idx]
        else:
            sp = np.zeros((0, 2), dtype="<i4")
        keys.append(write_tile(tid, ids, leu, lev, lf[edges], lstart, sp))
        written.add(tid)
    empty = np.zeros(0, dtype=np.int64)
    for tid in sorted(poi_by_tile):   # tiles with amenities but no streets of their own
        if tid not in written:
            keys.append(write_tile(tid, empty, np.zeros(0, "<u4"), np.zeros(0, "<u4"), np.zeros(0, "<u4"),
                                   np.zeros(1, "<u4"), np.zeros((0, 2), dtype="<i4")))
    return {"km": tile_km, "lon0": lon0, "lat0": lat0, "dlon": dlon, "dlat": dlat, "list": keys}


def collect_pois(gdf):
    """Points of interest from an OSMnx features GeoDataFrame, as [lon, lat, category, wheelchair] lists."""
    keep = [c for c in ("shop", "amenity", "leisure", "railway", "wheelchair") if c in gdf.columns]
    pts = []
    for _, row in gdf.iterrows():
        tags = {k: row[k] for k in keep if isinstance(row[k], str)}
        result = categorize(tags)
        geom = row.geometry
        if result is None or geom is None or geom.is_empty:
            continue
        p = geom if geom.geom_type == "Point" else geom.representative_point()
        pts.append([round(p.x, 5), round(p.y, 5), result[0], result[1]])
    return pts


def write_pois_json(pts, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"cats": CATS, "pts": pts}, f, ensure_ascii=False, separators=(",", ":"))


def export_pois(gdf, out_path):
    """Write points of interest from an OSMnx features GeoDataFrame."""
    pts = collect_pois(gdf)
    write_pois_json(pts, out_path)
    return len(pts)


def with_retries(fn, what, ox=None, servers=None, tries=6, waits=(5, 15, 45, 90, 120)):
    """Call fn(). If the server refuses or drops the connection (the free servers do that when busy, when they
    block an address for a while, or when an answer is very large), wait, move on to the next server in
    `servers` and try again, up to `tries` times in total. The server that works stays in use afterwards."""
    import requests
    retry_on = [requests.exceptions.RequestException]
    try:   # osmnx raises its own errors when a server answers with an error page (for example "500 Internal Server Error")
        from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError
        retry_on += [InsufficientResponseError, ResponseStatusCodeError]
    except ImportError:
        pass
    for attempt in range(tries):
        try:
            return fn()
        except tuple(retry_on) as err:
            if attempt == tries - 1:
                raise
            wait = waits[min(attempt, len(waits) - 1)]
            if getattr(getattr(err, "response", None), "status_code", None) == 429:   # asked too often: give it time
                wait = max(wait, 60)
            switched = ""
            if ox is not None and servers and len(servers) > 1:
                current = ox.settings.overpass_url
                ox.settings.overpass_url = servers[(servers.index(current) + 1) % len(servers)] if current in servers else servers[0]
                switched = ", switching to " + urlparse(ox.settings.overpass_url).netloc
            print("%s failed (%s: %s)%s; trying again in %d s (attempt %d of %d) ..."
                  % (what, type(err).__name__, " ".join(str(err).split())[:140], switched, wait, attempt + 2, tries))
            time.sleep(wait)


def walk_exclusion(tags):
    """Why the walking network leaves out a way: a short reason, or None if the way is kept. These are the rules
    OSMnx uses for its "walk" network (and sends to Overpass): a way with a highway tag, except areas, private
    access, and highway types that are not for walking; ways with foot=no; and ways whose sidewalks are mapped as
    separate ways. A missing tag never excludes a way. Like on the server, the exclusions are text matches (so
    "motor" also excludes motorway and motorway_link). One difference: a cycleway that is tagged foot=yes,
    designated or permissive (a shared foot and cycle path, which is how many bridge sidewalks are mapped) is kept."""
    hw = tags.get("highway")
    if hw is None:
        return "no highway tag"
    shared_path = hw == "cycleway" and tags.get("foot") in ("yes", "designated", "permissive")   # a cycle path that people on foot may use too
    if not shared_path and re.search("abandoned|bus_guideway|construction|cycleway|motor|no|planned|platform|proposed|raceway|razed|rest_area|services", hw):
        return "highway=%s is not part of the walking network" % hw
    for key, bad in (("area", "yes"), ("access", "private"), ("service", "private"), ("foot", "no")):
        if re.search(bad, tags.get(key, "")):
            return "%s=%s" % (key, tags[key])
    for key in ("sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right"):
        if re.search("separate", tags.get(key, "")):
            return "%s=%s (the sidewalk is expected to be mapped as its own way)" % (key, tags[key])
    return None


def walkable_way(tags):
    return walk_exclusion(tags) is None


def graph_from_pbf(path, area, retain_all=False):
    """The walking network from a downloaded extract (.osm.pbf) instead of an Overpass server, as an OSMnx-style
    networkx graph that is not simplified yet (every way is cut into one edge per pair of neighbouring nodes,
    both directions, with its length in metres). Ways that touch `area` (a shapely polygon in lon/lat) are read;
    nodes outside it are dropped, and unless retain_all is set only the largest connected piece is kept, as with
    the server route. Needs: pip install osmium"""
    try:
        import osmium
    except ImportError:
        raise SystemExit("Reading a .osm.pbf file needs the package osmium. Install it with:  pip install osmium")
    import networkx as nx
    from shapely.geometry import Point
    from shapely.prepared import prep
    minx, miny, maxx, maxy = area.bounds
    reader = osmium.FileProcessor(str(path)).with_locations().with_filter(osmium.filter.KeyFilter("highway"))
    ways = []
    for obj in reader:
        if not isinstance(obj, osmium.osm.Way):
            continue
        tags = {t.k: t.v for t in obj.tags}
        if not walkable_way(tags):
            continue
        pts = [(n.ref, n.location.lon, n.location.lat) for n in obj.nodes if n.location.valid()]
        if len(pts) < 2 or not any(minx <= x <= maxx and miny <= y <= maxy for _, x, y in pts):
            continue
        attrs = {"osmid": obj.id, "highway": tags["highway"]}
        if "ramp:wheelchair" in tags:
            attrs["ramp:wheelchair"] = tags["ramp:wheelchair"]
        ways.append((attrs, pts))
    print("Extract: %d walkable ways touch the area" % len(ways))
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    for attrs, pts in ways:
        for (a, ax, ay), (b, bx, by) in zip(pts, pts[1:]):
            if a == b:
                continue
            G.add_node(a, x=ax, y=ay)
            G.add_node(b, x=bx, y=by)
            length = haversine_m(ax, ay, bx, by)
            G.add_edge(a, b, length=length, **attrs)   # walking is possible in both directions
            G.add_edge(b, a, length=length, **attrs)
    inside = prep(area)
    outside = [n for n, d in G.nodes(data=True)
               if not (minx <= d["x"] <= maxx and miny <= d["y"] <= maxy and inside.contains(Point(d["x"], d["y"])))]
    G.remove_nodes_from(outside)
    print("Network: %d nodes, %d edges inside the area" % (G.number_of_nodes(), G.number_of_edges()))
    if not retain_all and G.number_of_nodes():
        biggest = max(nx.weakly_connected_components(G), key=len)
        G = G.subgraph(biggest).copy()
        print("Largest connected piece: %d nodes" % G.number_of_nodes())
    if G.number_of_nodes() == 0:
        raise SystemExit("No streets found inside the area. Does the extract cover it?")
    return G


def polygon_pieces(polygon, chunk_km):
    """Cut a (multi)polygon into pieces by a grid of about chunk_km kilometres. The pieces do not overlap and
    together cover the whole polygon. chunk_km of 0 (or less) keeps it as one piece."""
    from shapely.geometry import MultiPolygon, Polygon, box
    if not chunk_km or chunk_km <= 0:
        return [polygon]
    minx, miny, maxx, maxy = polygon.bounds
    dlat = chunk_km * 1000 / 110540.0
    dlon = chunk_km * 1000 / (111320.0 * math.cos(math.radians((miny + maxy) / 2)))
    pieces = []
    for ix in range(max(1, math.ceil((maxx - minx) / dlon))):
        for iy in range(max(1, math.ceil((maxy - miny) / dlat))):
            part = polygon.intersection(box(minx + ix * dlon, miny + iy * dlat, minx + (ix + 1) * dlon, miny + (iy + 1) * dlat))
            if part.is_empty:
                continue
            polys = list(part.geoms) if hasattr(part, "geoms") else [part]
            polys = [g for g in polys if isinstance(g, Polygon) and g.area > 0]
            if polys:
                pieces.append(polys[0] if len(polys) == 1 else MultiPolygon(polys))
    return pieces


class TooHeavy(Exception):
    """The server could not finish a request in time (504, timeout, 'runtime error'). Asking the same thing
    again, or asking another server, would fail the same way; a smaller box is the answer."""


def amenity_query(bbox, tags, timeout):
    """Overpass query for the amenities, benches and stations in one box (south, west, north, east): exact
    key=value matches, which the server answers faster than patterns, and only tags plus one centre point
    per object, not whole outlines."""
    box = "(%.6f,%.6f,%.6f,%.6f)" % bbox
    lines = ['nwr["%s"="%s"]%s;' % (key, value, box) for key, values in tags.items() for value in values]
    return "[out:json][timeout:%d];\n(\n%s\n);\nout tags center qt;" % (timeout, "\n".join(lines))


def overpass_amenities(ox, bbox, tags, timeout=60):
    """The amenities, benches and stations inside one box as raw OpenStreetMap elements, from the server in
    ox.settings.overpass_url. Answers are saved in cache/amenities, so a repeated run does not ask again.
    Raises TooHeavy if the server cannot finish in `timeout` seconds."""
    import requests
    query = amenity_query(bbox, tags, timeout)
    cache = Path("cache") / "amenities"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / (hashlib.sha1(query.encode("utf-8")).hexdigest() + ".json")
    if cached.exists():
        return json.load(open(cached, encoding="utf-8"))
    url = ox.settings.overpass_url.rstrip("/") + "/interpreter"
    # public servers turn away requests that do not say who is asking: send the same headers osmnx sends
    headers = {"User-Agent": ox.settings.http_user_agent, "Referer": ox.settings.http_referer,
               "Accept-Language": ox.settings.http_accept_language}
    # osmnx pins overpass-api.de to one of its machines for the whole run; if that machine is in trouble, every
    # request would keep going there. Use the normal lookup for this request instead.
    pinned = socket.getaddrinfo
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    try:
        response = requests.post(url, data={"data": query}, headers=headers, timeout=(15, timeout + 30))
    except requests.exceptions.ReadTimeout as err:
        raise TooHeavy(str(err))
    finally:
        socket.getaddrinfo = pinned
    if response.status_code == 504:
        raise TooHeavy("504 Gateway Timeout")
    response.raise_for_status()   # 429 and others: an ordinary failure, retried by with_retries
    data = response.json()
    remark = data.get("remark", "")
    if "runtime error" in remark or "timed out" in remark:   # the server gave up and sent a partial answer
        raise TooHeavy("Overpass: " + remark)
    with open(cached, "w", encoding="utf-8") as f:
        json.dump(data.get("elements", []), f, separators=(",", ":"))
    time.sleep(1.0)   # be polite to the free servers between requests
    return data.get("elements", [])


def amenities_from_pbf(path, polygon):
    """Points of interest from a downloaded OpenStreetMap extract (.osm.pbf, for example from
    download.geofabrik.de) as [lon, lat, category, wheelchair] lists, without asking any server. Uses the same
    rules (categorize) as the other routes, so the categories and the wheelchair tags are identical. Ways and
    multipolygon relations (large parks) are placed at the centre of their bounding box. Only objects inside
    the polygon are kept. Needs the package osmium (pip install osmium)."""
    try:
        import osmium
    except ImportError:
        raise SystemExit("Reading a .osm.pbf file needs the package osmium. Install it with:  pip install osmium")
    from shapely.geometry import Point
    from shapely.prepared import prep
    inside = prep(polygon)
    keys = ("shop", "amenity", "leisure", "railway")
    reader = (osmium.FileProcessor(str(path))
              .with_locations()
              .with_areas(osmium.filter.KeyFilter(*keys))   # relations that could become areas: only those with our keys
              .with_filter(osmium.filter.KeyFilter(*keys)))
    pts, seen, total = [], set(), 0
    for obj in reader:
        if isinstance(obj, osmium.osm.Node):
            kind = "n"
            locations = [obj.location] if obj.location.valid() else []
        elif isinstance(obj, osmium.osm.Way):
            kind = "w"
            locations = [n.location for n in obj.nodes if n.location.valid()]
        elif isinstance(obj, osmium.osm.Area) and not obj.from_way():   # closed ways already came as ways
            kind = "r"
            locations = [n.location for ring in obj.outer_rings() for n in ring if n.location.valid()]
        else:
            continue
        key = (kind, obj.orig_id() if kind == "r" else obj.id)
        if key in seen or not locations:
            continue
        seen.add(key)
        result = categorize({t.k: t.v for t in obj.tags})
        if result is None:
            continue
        lons, lats = [l.lon for l in locations], [l.lat for l in locations]
        lon, lat = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2   # centre of the bounding box
        total += 1
        if inside.contains(Point(lon, lat)):
            pts.append([round(lon, 5), round(lat, 5), result[0], result[1]])
    print("Extract: %d objects of interest, %d inside the area" % (total, len(pts)))
    return pts


def split_piece(piece):
    """A (multi)polygon cut into its four quarters (empty quarters left out)."""
    from shapely.geometry import MultiPolygon, Polygon, box
    minx, miny, maxx, maxy = piece.bounds
    mx, my = (minx + maxx) / 2, (miny + maxy) / 2
    quarters = []
    for b in (box(minx, miny, mx, my), box(mx, miny, maxx, my), box(minx, my, mx, maxy), box(mx, my, maxx, maxy)):
        part = piece.intersection(b)
        polys = [g for g in (list(part.geoms) if hasattr(part, "geoms") else [part]) if isinstance(g, Polygon) and g.area > 0]
        if polys:
            quarters.append(polys[0] if len(polys) == 1 else MultiPolygon(polys))
    return quarters


def download_amenity_points(ox, polygon, tags, chunk_km=8.0, servers=None):
    """Points of interest inside a (multi)polygon as [lon, lat, category, wheelchair] lists, fetched box by box
    (see overpass_amenities). It starts with boxes of about chunk_km kilometres; a box that is too heavy for the
    server (dense city centres) is split into four and the parts are tried again, so sparse areas are quick
    and dense ones get small boxes. Other failures are retried, switching servers. Objects found in two
    boxes are kept once, and objects outside the polygon are dropped."""
    from collections import deque
    from shapely.geometry import Point
    from shapely.prepared import prep
    todo = deque((piece, 0) for piece in polygon_pieces(polygon, chunk_km))
    inside = prep(polygon)
    pts, seen, done, heavy_retries = [], set(), 0, 0
    while todo:
        piece, depth = todo.popleft()
        minx, miny, maxx, maxy = piece.bounds
        print("Amenities: %d boxes done, %d waiting (box now: %.1f km wide) ..."
              % (done, len(todo) + 1, (maxx - minx) * 111.32 * math.cos(math.radians((miny + maxy) / 2))))
        try:
            elements = with_retries(lambda: overpass_amenities(ox, (miny, minx, maxy, maxx), tags),
                                    "Amenity download", ox=ox, servers=servers)
        except TooHeavy as err:
            if min(maxx - minx, maxy - miny) >= 0.02:   # larger than about 2 km: split it
                print("  that box is too heavy for the server (%s): splitting it into four" % err)
                for quarter in reversed(split_piece(piece)):
                    todo.appendleft((quarter, depth + 1))
                continue
            heavy_retries += 1   # already small: the server may just be overloaded; wait and try again
            if heavy_retries > 5:
                raise
            print("  a small box still timed out (%s); waiting 60 s and trying again" % err)
            time.sleep(60)
            todo.appendleft((piece, depth))
            continue
        done += 1
        for el in elements:
            key = (el.get("type"), el.get("id"))
            if key in seen:
                continue
            seen.add(key)
            point = el if el.get("type") == "node" else (el.get("center") or {})
            lon, lat = point.get("lon"), point.get("lat")
            result = categorize(el.get("tags") or {})
            if result is None or lon is None or lat is None or not inside.contains(Point(lon, lat)):
                continue
            pts.append([round(lon, 5), round(lat, 5), result[0], result[1]])
    return pts


def update_cities(data_dir, slug, name):
    """Add (or rename) this city in data/cities.json, which fills the page's city menu."""
    path = Path(data_dir) / "cities.json"
    cities = []
    if path.exists():
        try:
            cities = json.load(open(path, encoding="utf-8")).get("cities", [])
        except Exception:
            cities = []
    else:   # no list yet: start it from the city folders that already exist, so none of them drops out of the menu
        for d in sorted(Path(data_dir).iterdir()):
            if d.is_dir() and d.name != slug and (d / "meta.json").exists():
                try:
                    nm = json.load(open(d / "meta.json", encoding="utf-8")).get("name", d.name)
                except Exception:
                    nm = d.name
                cities.append({"id": d.name, "name": nm})
    cities = [c for c in cities if c.get("id") != slug] + [{"id": slug, "name": name}]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cities": cities}, f, ensure_ascii=False, indent=1)
    return [c["id"] for c in cities]


def main():
    ap = argparse.ArgumentParser(description="Export a walking network and amenities for the 15-minute page.")
    ap.add_argument("place", help='place name, e.g. "Köln, Germany"')
    ap.add_argument("slug", help="output folder name under data/, e.g. cologne")
    ap.add_argument("--radius", type=int, help="only export this many metres around the place centre (much lighter for the servers)")
    ap.add_argument("--center", help='latitude,longitude, for example "19.35,-99.14": the centre of the square when --radius is used (instead of looking the place up), otherwise only the point where the page starts')
    ap.add_argument("--chunk-km", type=float, default=8.0, help="for a whole place, download the amenities box by box, starting with boxes about this many kilometres wide (default 8; 0 = one request); a box that is too heavy for the server is split into four automatically")
    ap.add_argument("--keep-all", action="store_true", help="keep every connected piece of the street network, not only the largest one; the unconnected pieces then show up (grey) in the page's 'Show all streets in the data' layer, which helps to find gaps")
    ap.add_argument("--pbf", help="read the amenities from a downloaded extract (.osm.pbf, e.g. greater-london-latest.osm.pbf from download.geofabrik.de) instead of asking Overpass; needs: pip install osmium")
    ap.add_argument("--pbf-streets", action="store_true", help="with --pbf: take the walking network from the extract as well, so no Overpass server is needed at all (use this when the servers keep failing)")
    ap.add_argument("--osm-id", help='OpenStreetMap relation of the boundary to export, for example R1234567, if the place name picks the wrong area (the place name is then only a label); not with --radius')
    ap.add_argument("--overpass", help="Overpass server(s) to use, separated by commas, in the order to try them, e.g. https://overpass.private.coffee/api (default: three public servers, tried in turn when one fails)")
    ap.add_argument("--tile-km", type=float, help="cut the data into square tiles of this size (about 2.5 is a good start); the page then loads only the tiles around the point you click, which makes big areas possible")
    ap.add_argument("--name", help="name shown in the page's city menu (default: the place name)")
    args = ap.parse_args()
    if args.pbf and not Path(args.pbf).is_file():
        ap.error("--pbf: file not found: %s" % args.pbf)
    if args.osm_id and args.radius:
        ap.error("--osm-id exports a whole boundary, so it cannot be combined with --radius")
    manual_center = None
    if args.center:
        try:
            lat_s, lon_s = args.center.split(",")
            manual_center = (float(lat_s), float(lon_s))
            assert -90 <= manual_center[0] <= 90 and -180 <= manual_center[1] <= 180
        except (ValueError, AssertionError):
            ap.error('--center must look like "19.35,-99.14" (latitude,longitude in degrees)')

    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.log_console = True
    servers = [u.strip().rstrip("/") for u in args.overpass.split(",") if u.strip()] if args.overpass else list(OVERPASS_SERVERS)
    ox.settings.overpass_url = servers[0]
    ox.settings.overpass_rate_limit = False   # do not wait 60 s whenever a server's status page cannot be reached; with_retries handles busy servers
    # osmnx drops most way tags; keep the one that marks stairs with a wheelchair ramp
    ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way) | {"ramp:wheelchair"})
    out = Path("data") / args.slug
    out.mkdir(parents=True, exist_ok=True)
    tags = {"shop": sorted(GROCERY), "amenity": sorted(HEALTH | EDUCATION | EATING | {"bench"}),
            "leisure": sorted(PARKS), "railway": ["station", "tram_stop"]}

    center, polygon = None, None
    if args.radius:
        center = manual_center or ox.geocode(args.place)   # (lat, lon)
    else:
        place_gdf = ox.geocode_to_gdf(args.osm_id, by_osmid=True) if args.osm_id else ox.geocode_to_gdf(args.place)
        polygon = place_gdf.geometry.iloc[0]
        if polygon.geom_type not in ("Polygon", "MultiPolygon"):
            raise SystemExit("The name '%s' found a point, not an area. Try a more specific name or use --osm-id." % args.place)
        shown = place_gdf["display_name"].iloc[0] if "display_name" in place_gdf.columns else args.place
        area_km2 = ox.projection.project_gdf(place_gdf).geometry.area.iloc[0] / 1e6
        n_points = count_vertices(polygon)
        print("Boundary: %s (about %s km², %d boundary points) - press Ctrl+C now if this is not the area you want"
              % (shown, format(round(area_km2), ","), n_points))
        if n_points > 5000:   # a very detailed boundary makes the request to the server heavy
            polygon = polygon.simplify(0.0003, preserve_topology=True)
            print("Boundary simplified to %d points (the shape changes by less than about 30 m)." % count_vertices(polygon))

    # amenities first: this is the part the free servers refuse most often, so a problem shows up in minutes
    # instead of after the long street processing (and the answers are cached)
    if args.pbf:
        print("Reading amenities from %s ..." % args.pbf)
        if args.radius:   # the square around the centre, in degrees
            from shapely.geometry import box
            dlat = args.radius / 110540.0
            dlon = args.radius / (111320.0 * math.cos(math.radians(center[0])))
            area = box(center[1] - dlon, center[0] - dlat, center[1] + dlon, center[0] + dlat)
        else:
            area = polygon
        pts = amenities_from_pbf(args.pbf, area)
    elif args.radius:
        print("Downloading amenities ...")
        gdf = with_retries(lambda: ox.features_from_point(center, tags, dist=args.radius), "Amenity download", ox=ox, servers=servers)
        pts = collect_pois(gdf)
    else:
        print("Downloading amenities ...")
        pts = download_amenity_points(ox, polygon, tags, args.chunk_km, servers)
    print("Points of interest:", len(pts))

    if args.pbf_streets:
        if not args.pbf:
            raise SystemExit("--pbf-streets needs --pbf FILE")
        print("Reading the walking network from %s ..." % args.pbf)
        G = graph_from_pbf(args.pbf, area, args.keep_all)
    else:
        print("Downloading the walking network ... (a busy server answers 504 and the script retries; that can take a while)")
        if args.radius:
            G = with_retries(lambda: ox.graph_from_point(center, dist=args.radius, dist_type="bbox", network_type="walk", simplify=False, retain_all=args.keep_all), "Street download", ox=ox, servers=servers)
        else:
            G = with_retries(lambda: ox.graph_from_polygon(polygon, network_type="walk", simplify=False, retain_all=args.keep_all), "Street download", ox=ox, servers=servers)
    try:
        G = ox.simplify_graph(G, edge_attrs_differ=["highway", "ramp:wheelchair"])   # keeps steps (and ramped steps) as their own edges
    except TypeError:
        print("Note: this OSMnx version cannot keep steps separate; edges containing steps are blocked whole.")
        G = ox.simplify_graph(G)
    arrays = build_graph_arrays(G)
    stats = graph_summary(arrays)
    print("Graph:", stats)
    print("Steps: %d that block stair-free routes, %d with a wheelchair ramp tag (counted as passable)"
          % (stats["steps"], stats["steps_with_wheelchair_ramp"]))

    minlon, minlat, maxlon, maxlat = stats["bbox"]
    start = list(manual_center or center) if (manual_center or center) else [(minlat + maxlat) / 2, (minlon + maxlon) / 2]
    if not (manual_center or center):
        try:
            start = list(ox.geocode(args.place))
        except Exception as err:   # geocoding is optional
            print("Geocoding failed, using the bounding box centre:", err)
    meta = {"name": args.place, "bbox": stats["bbox"], "start": start,
            "nodes": stats["nodes"], "edges": stats["edges"], "pois": len(pts)}

    if args.tile_km:
        meta["tiles"] = write_tiles(arrays, pts, out, args.tile_km)
        meta["cats"] = CATS
        print("Tiles:", len(meta["tiles"]["list"]))
    else:
        write_graph_bin(arrays, out / "graph.bin")
        write_pois_json(pts, out / "pois.json")
    with open(out / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    print("City menu now lists:", update_cities("data", args.slug, args.name or args.place))

    if args.tile_km:
        sizes = [p.stat().st_size for p in (out / "t").glob("*.bin")]
        print("tiles: %d files, %.2f MB in total, largest %.2f MB" % (len(sizes), sum(sizes) / 1e6, max(sizes) / 1e6))
    else:
        for name in ("graph.bin", "pois.json"):
            print(f"{name}: {(out / name).stat().st_size / 1e6:.2f} MB")
    print(f"meta.json: {(out / 'meta.json').stat().st_size / 1e6:.2f} MB")
    print("Done. Put index.html next to the data/ folder and serve it (python -m http.server 8000).")


if __name__ == "__main__":
    main()
