#!/usr/bin/env python3
"""Export a walking network and points of interest from OpenStreetMap for the
"15 minutes. For whom?" page.

Usage:
    pip install osmnx
    python export_city.py "Köln, Germany" cologne
    python export_city.py "Köln, Germany" cologne --radius 6000     # smaller area, lighter download
    python export_city.py "Köln, Germany" cologne --overpass https://overpass.private.coffee/api
    python export_city.py "Shibuya Station, Tokyo, Japan" tokyo --name "Tokyo (Shibuya)" --radius 8000 --tile-km 2.5

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
import json
import math
import struct
from pathlib import Path

import numpy as np

CATS = ["Groceries", "Health", "Education", "Parks & play", "Cafés & eating", "Benches", "Stations"]
GROCERY = {"supermarket", "convenience", "greengrocer", "bakery", "butcher"}
HEALTH = {"pharmacy", "doctors", "clinic", "hospital", "dentist"}
EDUCATION = {"school", "kindergarten", "library"}
PARKS = {"park", "playground", "garden"}
EATING = {"cafe", "restaurant", "pub", "bar"}
WHEELCHAIR = {"yes": 1, "limited": 2, "no": 3}   # 0 = not tagged


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
    Flag bit 0 = the edge is a flight of steps. Shape points are the bends between the two end nodes.
    """
    nodes = list(G.nodes)
    index = {n: i for i, n in enumerate(nodes)}
    lonlat = np.array([(e5(G.nodes[n]["x"]), e5(G.nodes[n]["y"])) for n in nodes], dtype=np.int32).reshape(-1, 2)

    best = {}   # (low node, high node, steps) -> (length in m, interior points low -> high)
    for u, v, _key, data in G.edges(keys=True, data=True):
        a, b = index[u], index[v]
        if a == b:
            continue
        steps = is_steps(data.get("highway"))
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
        key = (min(a, b), max(a, b), steps)
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
        a, b, steps = key
        length, interior = best[key]
        eu[i], ev[i] = a, b
        lf[i] = (min(int(round(length * 10)), (1 << 30) - 1) << 2) | (1 if steps else 0)
        shape_pts.extend(interior)
        shape_start[i + 1] = len(shape_pts)
    shape_arr = np.array(shape_pts, dtype="<i4").reshape(-1, 2)
    return {"lonlat": lonlat, "eu": eu, "ev": ev, "lf": lf, "shape_start": shape_start, "shape_arr": shape_arr}


def graph_summary(a):
    lonlat = a["lonlat"]
    return {"nodes": len(lonlat), "edges": len(a["eu"]), "shape_points": len(a["shape_arr"]),
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
    ap.add_argument("--overpass", help="use another Overpass server, e.g. https://overpass.private.coffee/api")
    ap.add_argument("--tile-km", type=float, help="cut the data into square tiles of this size (about 2.5 is a good start); the page then loads only the tiles around the point you click, which makes big areas possible")
    ap.add_argument("--name", help="name shown in the page's city menu (default: the place name)")
    args = ap.parse_args()

    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.log_console = True
    if args.overpass:
        ox.settings.overpass_url = args.overpass
    out = Path("data") / args.slug
    out.mkdir(parents=True, exist_ok=True)
    tags = {"shop": sorted(GROCERY), "amenity": sorted(HEALTH | EDUCATION | EATING | {"bench"}),
            "leisure": sorted(PARKS), "railway": ["station", "tram_stop"]}

    center = None
    print("Downloading the walking network ... (a busy server answers 504 and the script retries; that can take a while)")
    if args.radius:
        center = ox.geocode(args.place)   # (lat, lon)
        G = ox.graph_from_point(center, dist=args.radius, dist_type="bbox", network_type="walk", simplify=False)
    else:
        G = ox.graph_from_place(args.place, network_type="walk", simplify=False)
    try:
        G = ox.simplify_graph(G, edge_attrs_differ=["highway"])   # keeps steps as their own edges
    except TypeError:
        print("Note: this OSMnx version cannot keep steps separate; edges containing steps are blocked whole.")
        G = ox.simplify_graph(G)
    arrays = build_graph_arrays(G)
    stats = graph_summary(arrays)
    print("Graph:", stats)

    print("Downloading amenities ...")
    if args.radius:
        gdf = ox.features_from_point(center, tags, dist=args.radius)
    else:
        gdf = ox.features_from_place(args.place, tags)
    pts = collect_pois(gdf)
    print("Points of interest:", len(pts))

    minlon, minlat, maxlon, maxlat = stats["bbox"]
    start = list(center) if center else [(minlat + maxlat) / 2, (minlon + maxlon) / 2]
    if not center:
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
