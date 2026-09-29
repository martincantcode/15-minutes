#!/usr/bin/env python3
"""Export a walking network and points of interest from OpenStreetMap for the
"15 minutes. For whom?" page.

Usage:
    pip install osmnx
    python export_city.py "Köln, Germany" cologne
    python export_city.py "Köln, Germany" cologne --radius 6000     # smaller area, lighter download
    python export_city.py "Köln, Germany" cologne --overpass https://overpass.private.coffee/api

Output, in data/<slug>/:
    graph.bin   street network (binary, read directly by the page)
    pois.json   amenities, benches, stations
    meta.json   name, bounding box, start point

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


def export_graph(G, out_path, simplify_tol_deg=3e-5):
    """Write a networkx (OSMnx-style) graph as one compact binary file.

    Layout, little endian: 4 x uint32 header (version, nodes, edges, shape points), then
    node lon/lat (int32, degrees * 1e5), edge from / to / (length in decimetres << 2 | flags)
    (uint32 each), shape start offsets (uint32, edges + 1) and shape points (int32 lon/lat pairs).
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

    with open(out_path, "wb") as f:
        f.write(struct.pack("<4I", 1, len(nodes), n_edges, len(shape_arr)))
        for arr in (lonlat.astype("<i4"), eu, ev, lf, shape_start, shape_arr):
            f.write(arr.tobytes())
    return {"nodes": len(nodes), "edges": n_edges, "shape_points": len(shape_arr),
            "bbox": [float(lonlat[:, 0].min() / 1e5), float(lonlat[:, 1].min() / 1e5),
                     float(lonlat[:, 0].max() / 1e5), float(lonlat[:, 1].max() / 1e5)]}


def export_pois(gdf, out_path):
    """Write points of interest from an OSMnx features GeoDataFrame."""
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
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"cats": CATS, "pts": pts}, f, ensure_ascii=False, separators=(",", ":"))
    return len(pts)


def main():
    ap = argparse.ArgumentParser(description="Export a walking network and amenities for the 15-minute page.")
    ap.add_argument("place", help='place name, e.g. "Köln, Germany"')
    ap.add_argument("slug", help="output folder name under data/, e.g. cologne")
    ap.add_argument("--radius", type=int, help="only export this many metres around the place centre (much lighter for the servers)")
    ap.add_argument("--overpass", help="use another Overpass server, e.g. https://overpass.private.coffee/api")
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
    stats = export_graph(G, out / "graph.bin")
    print("Graph:", stats)

    print("Downloading amenities ...")
    if args.radius:
        gdf = ox.features_from_point(center, tags, dist=args.radius)
    else:
        gdf = ox.features_from_place(args.place, tags)
    n_pois = export_pois(gdf, out / "pois.json")
    print("Points of interest:", n_pois)

    minlon, minlat, maxlon, maxlat = stats["bbox"]
    start = list(center) if center else [(minlat + maxlat) / 2, (minlon + maxlon) / 2]
    if not center:
        try:
            start = list(ox.geocode(args.place))
        except Exception as err:   # geocoding is optional
            print("Geocoding failed, using the bounding box centre:", err)
    with open(out / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"name": args.place, "bbox": stats["bbox"], "start": start,
                   "nodes": stats["nodes"], "edges": stats["edges"], "pois": n_pois}, f, ensure_ascii=False)

    for name in ("graph.bin", "pois.json", "meta.json"):
        print(f"{name}: {(out / name).stat().st_size / 1e6:.2f} MB")
    print("Done. Put index.html next to the data/ folder and serve it (python -m http.server 8000).")


if __name__ == "__main__":
    main()
