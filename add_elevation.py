#!/usr/bin/env python3
"""Add terrain to a city that export_city.py has already exported: read the height of every street
junction from a digital elevation model (DEM) and write it to data/<slug>/elev.bin.
The street data is NOT downloaded again.

Usage:
    pip install numpy rasterio pyproj requests
    python add_elevation.py luzern --swissalti3d           # Switzerland: downloads swisstopo's 2 m model tile by tile
    python add_elevation.py cologne --dem path/to/dgm.tif  # any DEM you have as GeoTIFF(s); several files are fine
    python add_elevation.py cologne --dem tiles_folder/    # or a folder full of .tif files

What it writes:
    elev.bin     one int16 per junction, in decimetres (-32768 = no height known), in the order of the whole
                 city's junction numbers; little endian. For a tiled city that is the same numbering the
                 tiles carry (ids), so the page can look the heights up in any tile.
    meta.json    gets "terrain": {"file": "elev.bin", "source": "..."} so the page knows the file exists.

Why only junctions: the page works out the slope of a street from the height difference between its two ends.
That avoids the classic trap of terrain models, which show a bridge as a deep valley (or a tunnel as a hill):
only the ends of the street are looked up, and they are on the ground. A street that goes up and down between
two junctions looks flatter than it is - the same limit any junction-to-junction method has.
"""
import argparse
import json
import re
import struct
import sys
from pathlib import Path

import numpy as np

NODATA = -32768
BLOCK = 1024   # read the DEM in blocks of this many pixels, so a huge file never has to fit in memory


def read_city_nodes(city_dir):
    """Longitude/latitude (degrees) of every junction of the city, in the numbering of the whole city."""
    meta = json.load(open(city_dir / "meta.json", encoding="utf-8"))
    if "tiles" not in meta:   # version 1: one file, the junction number is simply the position in the file
        buf = (city_dir / "graph.bin").read_bytes()
        version, nn, ne, ns = struct.unpack_from("<4I", buf, 0)
        if version != 1:
            raise SystemExit("graph.bin has version %d, expected 1" % version)
        ll = np.frombuffer(buf, dtype="<i4", count=2 * nn, offset=16).reshape(nn, 2)
        return meta, ll[:, 0] / 1e5, ll[:, 1] / 1e5
    total = meta["nodes"]   # tiled: collect the junctions from all tiles; each carries its number in the whole city
    lon = np.full(total, np.nan)
    lat = np.full(total, np.nan)
    for key in meta["tiles"]["list"]:
        buf = (city_dir / "t" / (key + ".bin")).read_bytes()
        version, nn, ne, ns, npoi = struct.unpack_from("<5I", buf, 0)
        ids = np.frombuffer(buf, dtype="<u4", count=nn, offset=20)
        ll = np.frombuffer(buf, dtype="<i4", count=2 * nn, offset=20 + 4 * nn).reshape(nn, 2)
        lon[ids] = ll[:, 0] / 1e5
        lat[ids] = ll[:, 1] / 1e5
    if np.isnan(lon).any():
        print("Note: %d junction numbers do not occur in any tile (they get no height)." % int(np.isnan(lon).sum()))
    return meta, lon, lat


def bilinear(arr, r, c):
    """Height at fractional pixel positions (r, c) of a 2-D array; NaN if any of the four pixels is missing."""
    r0 = np.floor(r).astype(int)
    c0 = np.floor(c).astype(int)
    fr, fc = r - r0, c - c0
    r0 = np.clip(r0, 0, arr.shape[0] - 2)
    c0 = np.clip(c0, 0, arr.shape[1] - 2)
    return (arr[r0, c0] * (1 - fr) * (1 - fc) + arr[r0, c0 + 1] * (1 - fr) * fc
            + arr[r0 + 1, c0] * fr * (1 - fc) + arr[r0 + 1, c0 + 1] * fr * fc)


def sample_dem(path, lon, lat, z):
    """Fill z (metres, NaN = unknown) for the junctions that lie inside the DEM file `path`."""
    import rasterio
    from pyproj import Transformer
    with rasterio.open(path) as ds:
        todo = np.where(np.isnan(z) & np.isfinite(lon))[0]
        if len(todo) == 0:
            return 0
        x, y = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform(lon[todo], lat[todo])
        inv = ~ds.transform
        c, r = inv * (np.asarray(x), np.asarray(y))
        c -= 0.5   # pixel centres: a pixel's height belongs to its middle, not its corner
        r -= 0.5
        ok = (r >= 0) & (c >= 0) & (r <= ds.height - 1) & (c <= ds.width - 1)
        todo, r, c = todo[ok], r[ok], c[ok]
        if len(todo) == 0:
            return 0
        found = 0
        # group the junctions by block, read each block once (with a one-pixel margin for the interpolation)
        br, bc = (r // BLOCK).astype(int), (c // BLOCK).astype(int)
        for b in set(zip(br.tolist(), bc.tolist())):
            sel = (br == b[0]) & (bc == b[1])
            r0, c0 = max(b[0] * BLOCK - 1, 0), max(b[1] * BLOCK - 1, 0)
            h, w = min(BLOCK + 3, ds.height - r0), min(BLOCK + 3, ds.width - c0)
            arr = ds.read(1, window=rasterio.windows.Window(c0, r0, w, h)).astype("float32")
            if ds.nodata is not None:
                arr[arr == ds.nodata] = np.nan
            arr[arr < -500] = np.nan   # some files use very large negative numbers instead of a declared nodata value
            if arr.shape[0] < 2 or arr.shape[1] < 2:
                continue
            v = bilinear(arr, r[sel] - r0, c[sel] - c0)
            z[todo[sel]] = v
            found += int(np.isfinite(v).sum())
        return found


def swissalti3d_files(lon, lat, cache_dir):
    """Download swisstopo's swissALTI3D (2 m) for the area of the junctions; returns the file paths.
    The tiles are 1 km x 1 km. Where several years exist for one tile, the newest is used."""
    import requests
    from pyproj import Transformer
    ok = np.isfinite(lon)
    bbox = [float(lon[ok].min()), float(lat[ok].min()), float(lon[ok].max()), float(lat[ok].max())]
    e, n = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True).transform(lon[ok], lat[ok])
    needed = set(zip((np.floor(e / 1000)).astype(int).tolist(), (np.floor(n / 1000)).astype(int).tolist()))
    print("The junctions lie on %d different 1 km x 1 km tiles." % len(needed))
    url = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items"
    params = {"bbox": ",".join("%.5f" % v for v in bbox), "limit": 100}
    newest = {}   # "2665-1211" -> (year, tif url)
    while url:
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("features", []):
            m = re.match(r"swissalti3d_(\d{4})_(\d{4})-(\d{4})$", item["id"])
            if not m or (int(m.group(2)), int(m.group(3))) not in needed:
                continue
            tif = next((a["href"] for k, a in item["assets"].items() if k.endswith("_2_2056_5728.tif")), None)
            key = m.group(2) + "-" + m.group(3)
            if tif and (key not in newest or int(m.group(1)) > newest[key][0]):
                newest[key] = (int(m.group(1)), tif)
        url = next((l["href"] for l in data.get("links", []) if l.get("rel") == "next"), None)
        params = None   # the next link already contains everything
    missing = len(needed) - len(newest)
    if missing:
        print("Note: no tile found for %d of the 1 km squares (those junctions get no height)." % missing)
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (key, (year, href)) in enumerate(sorted(newest.items()), 1):
        p = cache_dir / href.rsplit("/", 1)[1]
        if not p.exists():
            print("  downloading tile %d of %d (%s) ..." % (i, len(newest), key), end="\r", flush=True)
            with requests.get(href, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(str(p) + ".part", "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            Path(str(p) + ".part").rename(p)
        paths.append(p)
    print()
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="the city's folder name under data/, e.g. luzern")
    ap.add_argument("--dem", nargs="+", help="GeoTIFF file(s) and/or folder(s) with GeoTIFFs")
    ap.add_argument("--swissalti3d", action="store_true", help="Switzerland only: download swisstopo's swissALTI3D (2 m, open data) automatically")
    ap.add_argument("--data", default="data", help="the data folder (default: data)")
    ap.add_argument("--cache", default="dem_cache", help="where downloaded elevation tiles are kept (default: dem_cache)")
    args = ap.parse_args()
    if not args.dem and not args.swissalti3d:
        ap.error("give --dem FILE_OR_FOLDER or --swissalti3d")
    try:
        import rasterio, pyproj  # noqa: F401
    except ImportError:
        sys.exit("Please run: pip install rasterio pyproj requests")

    city_dir = Path(args.data) / args.slug
    meta, lon, lat = read_city_nodes(city_dir)
    print("%d junctions" % len(lon))
    files, source = [], []
    if args.swissalti3d:
        files += swissalti3d_files(lon, lat, Path(args.cache) / "swissalti3d")
        source.append("swissALTI3D 2 m, swisstopo (Federal Office of Topography), open government data")
    for item in args.dem or []:
        p = Path(item)
        files += sorted(p.rglob("*.tif")) + sorted(p.rglob("*.tiff")) if p.is_dir() else [p]
    if not files:
        sys.exit("No elevation files found.")

    z = np.full(len(lon), np.nan)
    for i, f in enumerate(files, 1):
        got = sample_dem(f, lon, lat, z)
        print("  file %d of %d: %d junctions   " % (i, len(files), got), end="\r", flush=True)
    print()
    known = np.isfinite(z)
    print("Height found for %d of %d junctions (%.1f %%)." % (known.sum(), len(z), 100 * known.mean()))
    if known.mean() < 0.9:
        print("Warning: fewer than 90 %% have a height. The page treats a street with an unknown end as flat.")

    out = np.full(len(z), NODATA, dtype="<i2")
    out[known] = np.clip(np.round(z[known] * 10), -32000, 32000).astype("<i2")
    (city_dir / "elev.bin").write_bytes(out.tobytes())
    zz = z[known]
    print("Heights from %.0f m to %.0f m above sea level; elev.bin is %.2f MB." % (zz.min(), zz.max(), out.nbytes / 1e6))
    meta["terrain"] = {"file": "elev.bin", "source": "; ".join(source) if source else "digital elevation model (%d file(s))" % len(files)}
    with open(city_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    print("meta.json updated. Upload data/%s/elev.bin and data/%s/meta.json to GitHub." % (args.slug, args.slug))


if __name__ == "__main__":
    main()
