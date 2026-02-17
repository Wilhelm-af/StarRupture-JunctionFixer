#!/usr/bin/env python3
"""
StarRupture Junction Fix - Made by Wilhelm-af
=========================================

Per-junction fix with correct lane axis detection.

Usage:
  python fix_all_junctions.py <save.sav>           # dry run
  python fix_all_junctions.py <save.sav> --apply   # apply fix
  python fix_all_junctions.py <save.sav> -v         # verbose
"""

import json, zlib, struct, re, sys, shutil, argparse
from pathlib import Path
from collections import defaultdict


# ─── Save File I/O ───────────────────────────────────────────────────────────

def read_sav(path):
    with open(path, "rb") as f:
        raw = f.read()
    json_bytes = zlib.decompress(raw[4:])
    print(f"  Read: {path} ({len(raw):,} bytes, decompressed: {len(json_bytes):,})")
    return json.loads(json_bytes.decode("utf-8"))


def write_sav(path, data):
    json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    compressed = zlib.compress(json_bytes)
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(json_bytes)))
        f.write(compressed)
    print(f"  Wrote: {path} ({4 + len(compressed):,} bytes)")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def extract_id(key):
    m = re.search(r'\(ID=(\d+)\)', key)
    return int(m.group(1)) if m else None


def find_entity_container(data):
    if isinstance(data, dict):
        if any(re.match(r'\(ID=\d+\)', k) for k in data.keys()) and len(data) > 100:
            return data
        for v in data.values():
            r = find_entity_container(v)
            if r: return r
    elif isinstance(data, list):
        for v in data:
            r = find_entity_container(v)
            if r: return r
    return None


def get_config(entity):
    return entity.get("spawnData", {}).get("entityConfigDataPath", "")


def get_spline_data(entity):
    """Extract endpoints and positions from spline fragment."""
    for frag in entity.get("fragmentValues", []):
        if not isinstance(frag, str) or "AuSplineConnectionFragment" not in frag:
            continue
        s = re.search(r'StartEntity=\(ID=(\d+)\)', frag)
        e = re.search(r'EndEntity=\(ID=(\d+)\)', frag)
        if not s or not e:
            continue
        positions = re.findall(r'Position=\(X=([\-\d.]+),Y=([\-\d.]+)', frag)
        return {
            "start_id": int(s.group(1)),
            "end_id": int(e.group(1)),
            "start_pos": (float(positions[0][0]), float(positions[0][1])) if positions else None,
            "end_pos": (float(positions[-1][0]), float(positions[-1][1])) if positions else None,
        }
    return None


def rewrite_spline_field(entity, old_id, new_id, field):
    fragments = entity.get("fragmentValues", [])
    for fi, frag in enumerate(fragments):
        if not isinstance(frag, str) or "AuSplineConnectionFragment" not in frag:
            continue
        pattern = f'{field}Entity=\\(ID={old_id}\\)'
        replacement = f'{field}Entity=(ID={new_id})'
        new_frag = re.sub(pattern, replacement, frag)
        if new_frag != frag:
            fragments[fi] = new_frag
            return True
    return False


def make_pole_entity():
    return {
        "spawnData": {
            "entityConfigDataPath": "/Game/Chimera/Buildings/DroneConnections/InvisibleConnection/DA_DroneInvisiblePole.DA_DroneInvisiblePole",
            "transform": {
                "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
                "translation": {"x": 0, "y": 0, "z": 0},
                "scale3D": {"x": 1, "y": 1, "z": 1}
            }
        },
        "tags": [],
        "fragmentValues": [
            "/Script/Chimera.CrElectricityFragment(ElectricityMultiplierLevel=1)"
        ]
    }


def get_max_entity_id(entities):
    max_id = 0
    for key in entities:
        eid = extract_id(key)
        if eid and eid < 4294967295:
            max_id = max(max_id, eid)
    return max_id


def detect_lane_axis(positions):
    """
    Given a list of (x, y) positions that are on the SAME FACE of a junction,
    determine which axis separates the lanes.
    """
    if len(positions) < 2:
        return 'x'
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    x_spread = max(xs) - min(xs)
    y_spread = max(ys) - min(ys)
    return 'x' if x_spread > y_spread else 'y'


def cluster_by_value(items, tolerance=15.0):
    """
    Cluster (value, item) pairs by proximity of value.
    Returns list of clusters: [[item, ...], ...]
    """
    if not items:
        return []
    sorted_items = sorted(items, key=lambda x: x[0])
    clusters = [[sorted_items[0]]]
    for i in range(1, len(sorted_items)):
        if abs(sorted_items[i][0] - sorted_items[i-1][0]) <= tolerance:
            clusters[-1].append(sorted_items[i])
        else:
            clusters.append([sorted_items[i]])
    return clusters


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fix ALL junctions (universal v5)")
    parser.add_argument("save_file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    sav_path = Path(args.save_file)
    print("=" * 65)
    print("  StarRupture Junction Fix — Universal v5")
    print("  Per-junction, auto lane axis from neighbor groups")
    print("=" * 65)
    print(f"  Mode: {'APPLY' if args.apply else 'DRY RUN'}\n")

    data = read_sav(sav_path)
    entities = find_entity_container(data)
    if not entities:
        print("  ERROR: No entity container found")
        return

    # ── Discovery ──
    junction_ids = {}  # eid -> "3-way"/"5-way"
    splines = []       # list of spline info dicts
    pole_keys = []
    drone_keys = []

    for key, value in entities.items():
        eid = extract_id(key)
        if eid is None or not isinstance(value, dict):
            continue
        config = get_config(value)

        if "DroneLane_3" in config:
            junction_ids[eid] = "3-way"
        elif "DroneLane_5" in config:
            junction_ids[eid] = "5-way"
        if "DroneInvisiblePole" in config:
            pole_keys.append(key)
        if "RailDroneConfig" in config:
            drone_keys.append(key)

        sd = get_spline_data(value)
        if sd:
            sd["id"] = eid
            sd["key"] = key
            sd["entity"] = value
            splines.append(sd)

    junction_set = set(junction_ids.keys())
    print(f"  Junctions: {len(junction_ids)}")
    print(f"  Splines: {len(splines)}")
    print(f"  Old poles: {len(pole_keys)}")
    print(f"  Drones: {len(drone_keys)}")

    # ── Build per-junction spline index ──
    # For each junction: list of (spline_info, field, neighbor_id, pos_at_junction)
    junction_touches = defaultdict(list)

    for sp in splines:
        if sp["start_id"] in junction_set:
            neighbor = sp["end_id"]
            junction_touches[sp["start_id"]].append({
                "spline": sp,
                "field": "Start",
                "neighbor": neighbor,
                "pos": sp["start_pos"],
            })
        if sp["end_id"] in junction_set:
            neighbor = sp["start_id"]
            junction_touches[sp["end_id"]].append({
                "spline": sp,
                "field": "End",
                "neighbor": neighbor,
                "pos": sp["end_pos"],
            })

    # ── Process each junction ──
    next_id = get_max_entity_id(entities) + 1
    new_poles = {}
    all_changes = []  # (spline_entity, field, junction_id, pole_id, spline_id)
    junctions_fixed = 0
    junctions_skipped = 0

    for jid in sorted(junction_ids.keys()):
        touches = junction_touches.get(jid, [])
        if len(touches) < 2:
            junctions_skipped += 1
            continue

        # Group by neighbor to find the lane axis
        by_neighbor = defaultdict(list)
        for t in touches:
            by_neighbor[t["neighbor"]].append(t)

        # Find a neighbor group with 2+ splines to detect lane axis
        lane_axis = None
        for neighbor, group in by_neighbor.items():
            if len(group) >= 2:
                positions = [t["pos"] for t in group if t["pos"]]
                if len(positions) >= 2:
                    lane_axis = detect_lane_axis(positions)
                    break

        if lane_axis is None:
            # All neighbors have only 1 spline → single-lane connections, no fix needed
            junctions_skipped += 1
            continue

        # Cluster ALL touches by lane axis value
        lane_items = []
        for t in touches:
            if t["pos"]:
                val = t["pos"][0] if lane_axis == 'x' else t["pos"][1]
                lane_items.append((val, t))

        clusters = cluster_by_value(lane_items, tolerance=15.0)

        if len(clusters) <= 1:
            junctions_skipped += 1
            continue

        if args.verbose:
            print(f"\n  Junction {jid} ({junction_ids[jid]}): {len(touches)} splines → {len(clusters)} lanes (axis={lane_axis})")

        # One pole per lane cluster
        for lane_idx, cluster in enumerate(clusters):
            pole_id = next_id
            next_id += 1
            new_poles[pole_id] = make_pole_entity()

            if args.verbose:
                sp_ids = [item[1]["spline"]["id"] for item in cluster]
                avg_val = sum(item[0] for item in cluster) / len(cluster)
                print(f"    Lane {lane_idx+1} ({lane_axis}≈{avg_val:.0f}): pole {pole_id}, splines: {sp_ids}")

            for val, touch in cluster:
                sp = touch["spline"]
                field = touch["field"]
                all_changes.append((sp["entity"], field, jid, pole_id, sp["id"]))

        junctions_fixed += 1

    # ── Summary ──
    print(f"\n{'='*65}")
    print(f"  SUMMARY")
    print(f"{'='*65}")
    print(f"  Junctions fixed: {junctions_fixed}")
    print(f"  Junctions skipped (single-lane or unconnected): {junctions_skipped}")
    print(f"  Spline rewrites: {len(all_changes)}")
    print(f"  New poles: {len(new_poles)}")
    print(f"  Old poles to remove: {len(pole_keys)}")
    print(f"  Drones to remove: {len(drone_keys)}")

    if not args.apply:
        print(f"\n  DRY RUN. Use --apply to modify the save.")
        return

    # ── Apply ──
    print(f"\n  Applying...")

    # Create new poles
    for pole_id, pole_entity in new_poles.items():
        entities[f"(ID={pole_id})"] = pole_entity
    print(f"    ✓ Created {len(new_poles)} poles")

    # Rewrite splines (track to avoid duplicates)
    done = set()
    ok_count = 0
    skip_count = 0
    fail_count = 0
    for sp_entity, field, jid, pole_id, sp_eid in all_changes:
        key = (sp_eid, field)
        if key in done:
            skip_count += 1
            continue
        done.add(key)

        if rewrite_spline_field(sp_entity, jid, pole_id, field):
            ok_count += 1
        else:
            fail_count += 1
            if args.verbose:
                print(f"    ✗ FAILED: Spline {sp_eid}: {field} {jid}→{pole_id}")
    print(f"    ✓ Rewrote {ok_count} endpoints ({skip_count} skipped, {fail_count} failed)")

    # Remove old poles
    removed = 0
    for key in pole_keys:
        if key in entities:
            del entities[key]
            removed += 1
    print(f"    ✓ Removed {removed} old poles")

    # Remove drones
    for key in drone_keys:
        if key in entities:
            del entities[key]
    print(f"    ✓ Removed {len(drone_keys)} drones")

    # ── Backup and write ──
    backup_path = sav_path.with_suffix(".sav.backup")
    i = 0
    while backup_path.exists():
        i += 1
        backup_path = sav_path.with_suffix(f".sav.backup{i}")
    shutil.copy2(sav_path, backup_path)
    print(f"\n  Backup: {backup_path}")

    write_sav(sav_path, data)

    print(f"\n  ✓ Done! {junctions_fixed} junctions fixed")
    print(f"  ✓ Load the save and verify")


if __name__ == "__main__":
    main()
