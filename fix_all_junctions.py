#!/usr/bin/env python3
"""
StarRupture Junction Fix - Made by Wilhelm-af
=========================================

Per-junction fix with correct lane axis detection.

Usage:
  python fix_all_junctions.py <save.sav>                    # dry run
  python fix_all_junctions.py <save.sav> --apply            # apply fix
  python fix_all_junctions.py <save.sav> --revert           # dry run revert
  python fix_all_junctions.py <save.sav> --revert --apply   # apply revert
  python fix_all_junctions.py <save.sav> -v                 # verbose
"""

import json, zlib, struct, re, sys, shutil, argparse, copy
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
    json_bytes = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode("utf-8")
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
        if 'entities' in data:
            return data['entities']
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
        positions = re.findall(r'Position=\(X=([\-\d.]+),Y=([\-\d.]+),Z=([\-\d.]+)\)', frag)
        return {
            "start_id": int(s.group(1)),
            "end_id": int(e.group(1)),
            "start_pos": (float(positions[0][0]), float(positions[0][1]), float(positions[0][2])) if positions else None,
            "end_pos": (float(positions[-1][0]), float(positions[-1][1]), float(positions[-1][2])) if positions else None,
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


def rotate_by_quat(offset, q):
    """Rotate local offset (x,y,z) by quaternion (qx,qy,qz,qw)."""
    qx, qy, qz, qw = q
    ox, oy, oz = offset
    tx = 2 * (qy*oz - qz*oy)
    ty = 2 * (qz*ox - qx*oz)
    tz = 2 * (qx*oy - qy*ox)
    return (
        ox + qw*tx + (qy*tz - qz*ty),
        oy + qw*ty + (qz*tx - qx*tz),
        oz + qw*tz + (qx*ty - qy*tx),
    )


# DroneLane_3 local socket offsets: 3 lanes, each with side A and side B
# Verified across 5 entities in WorkingSave.sav (identity rotation)
DRONE_LANE_3_OFFSETS = [
    # Lane 0 (center): (side_a, side_b)
    ((0.000001, -54.186704, 307.937676), (0.011049, 54.179305, 307.218374)),
    # Lane 1 (left):
    ((-19.939297, -54.000000, 307.937676), (-20.000000, 54.000000, 307.218374)),
    # Lane 2 (right):
    ((19.991659, -54.000000, 307.937676), (20.000000, 54.000000, 307.218374)),
]

INVISIBLE_POLE_TEMPLATE = {
    "spawnData": {
        "entityConfigDataPath": "/Game/Chimera/Buildings/DroneConnections/InvisibleConnection/DA_DroneInvisiblePole.DA_DroneInvisiblePole",
        "transform": {
            "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
            "translation": {"x": 0, "y": 0, "z": 0},
            "scale3D": {"x": 1, "y": 1, "z": 1}
        }
    },
    "tags": [],
    "fragmentValues": ["/Script/Chimera.CrElectricityFragment(ElectricityMultiplierLevel=1)"]
}


def get_max_entity_id(entities):
    """Find the highest entity ID in the container."""
    max_id = 0
    for key in entities:
        eid = extract_id(key)
        if eid is not None and eid > max_id:
            max_id = eid
    return max_id


def match_spline_to_socket(socket_pos, touches, tolerance=15.0):
    """Find spline endpoint matching this socket position.
    Returns 'Input', 'Output', or None."""
    best_dist = tolerance
    best_type = None
    for t in touches:
        if not t["pos"]:
            continue
        dist = ((socket_pos[0] - t["pos"][0])**2 +
                (socket_pos[1] - t["pos"][1])**2 +
                (socket_pos[2] - t["pos"][2])**2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_type = "Output" if t["field"] == "Start" else "Input"
    return best_type


def build_socket_fragment(socket_positions):
    """Build CrLogisticsSocketsFragment string from socket positions.

    socket_positions: list of tuples, either:
      (x, y, z, connection_type) — basic socket
      (x, y, z, connection_type, pole_id) — socket with invisible pole pairing
    connection_type: 'Input', 'Output', or None
    pole_id: int or None
    """
    parts = []
    for item in socket_positions:
        x, y, z = item[0], item[1], item[2]
        ctype = item[3] if len(item) > 3 else None
        pole_id = item[4] if len(item) > 4 else None
        s = f"WorldPosition=(X={x:.6f},Y={y:.6f},Z={z:.6f})"
        if ctype:
            s += f",ConnectionType={ctype}"
            s += ",ConnectionEntity=()"
        if pole_id is not None:
            s += f",SocketPairInvisibleConnector=(ID={pole_id})"
        parts.append(f"({s})")
    return f"/Script/Chimera.CrLogisticsSocketsFragment(Sockets=({','.join(parts)}))"


def write_socket_fragment(entity, fragment_str):
    """Add or replace CrLogisticsSocketsFragment in entity's fragmentValues."""
    frags = entity.get("fragmentValues", [])
    for i, frag in enumerate(frags):
        if isinstance(frag, str) and "CrLogisticsSocketsFragment" in frag:
            frags[i] = fragment_str
            return "replaced"
    frags.append(fragment_str)
    return "added"


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


# ─── Revert ───────────────────────────────────────────────────────────────────

def revert_fix(data, apply=False, verbose=False):
    """Revert a previous junction fix by removing invisible poles and restoring spline endpoints."""
    entities = find_entity_container(data)
    if not entities:
        print("  ERROR: No entity container found")
        return None

    # Find all invisible poles with positions
    poles = {}  # eid -> (x, y, z)
    pole_keys = []
    for key, value in entities.items():
        eid = extract_id(key)
        if eid is None or not isinstance(value, dict):
            continue
        config = get_config(value)
        if "DroneInvisiblePole" in config:
            pole_keys.append(key)
            pos = value.get("spawnData", {}).get("transform", {}).get("translation", {})
            poles[eid] = (pos.get("x", 0), pos.get("y", 0), pos.get("z", 0))

    if not poles:
        print("  No invisible poles found. Nothing to revert.")
        return {"poles_removed": 0, "rewrites": 0}

    print(f"  Invisible poles found: {len(poles)}")

    # Find all junction/merger entities with positions
    junctions = {}  # eid -> (x, y, z)
    for key, value in entities.items():
        eid = extract_id(key)
        if eid is None or not isinstance(value, dict):
            continue
        config = get_config(value)
        if any(t in config for t in [
            'DroneLane_3', 'DroneLane_5',
            'DroneMerger_3To1', 'DroneMerger_5To1',
            'DA_DroneJunction_4'
        ]):
            pos = value.get("spawnData", {}).get("transform", {}).get("translation", {})
            junctions[eid] = (pos.get("x", 0), pos.get("y", 0), pos.get("z", 0))

    print(f"  Junctions/mergers found: {len(junctions)}")

    # Build spline index: find splines connected to poles
    pole_set = set(poles.keys())
    spline_rewrites = []  # (key, entity, field, pole_id, junction_id)

    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        sd = get_spline_data(value)
        if not sd:
            continue
        for field, eid_key in [("Start", "start_id"), ("End", "end_id")]:
            if sd[eid_key] in pole_set:
                pole_id = sd[eid_key]
                pole_pos = poles[pole_id]

                # Find nearest junction/merger
                best_jid = None
                best_dist = float('inf')
                for jid, jpos in junctions.items():
                    dist = ((pole_pos[0] - jpos[0])**2 +
                            (pole_pos[1] - jpos[1])**2 +
                            (pole_pos[2] - jpos[2])**2) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_jid = jid

                if best_jid and best_dist <= 500.0:
                    spline_rewrites.append((key, value, field, pole_id, best_jid))
                    if verbose:
                        print(f"    Spline {extract_id(key)}: {field} pole {pole_id} -> junction {best_jid} (dist={best_dist:.1f})")
                else:
                    if verbose:
                        if best_jid:
                            print(f"    WARNING: Pole {pole_id} nearest junction {best_jid} is {best_dist:.1f}u away (>500u)")
                        else:
                            print(f"    WARNING: Pole {pole_id} has no nearby junction")

    print(f"  Spline rewrites needed: {len(spline_rewrites)}")
    print(f"  Poles to delete: {len(pole_keys)}")

    if not apply:
        print(f"\n  DRY RUN. Use --apply to modify the save.")
        return {"poles_removed": len(pole_keys), "rewrites": len(spline_rewrites)}

    # Apply rewrites
    ok_count = 0
    fail_count = 0
    for _key, entity, field, pole_id, junction_id in spline_rewrites:
        if rewrite_spline_field(entity, pole_id, junction_id, field):
            ok_count += 1
        else:
            fail_count += 1
            if verbose:
                print(f"    x FAILED: {_key} {field} {pole_id}->{junction_id}")
    print(f"    Rewrote {ok_count} spline endpoints ({fail_count} failed)")

    # Delete invisible poles
    removed = 0
    pole_id_set = set(poles.keys())
    for key in pole_keys:
        if key in entities:
            del entities[key]
            removed += 1
    print(f"    Removed {removed} invisible poles")

    # Clean up electricity connectorData for removed poles
    cleaned_elec = 0
    elec_conn = data.get("itemData", {}).get("Mass", {}).get(
        "electricitySubsystemState", {}).get("connectorData", {})
    if isinstance(elec_conn, dict):
        keys_to_remove = [k for k in elec_conn if extract_id(k) in pole_id_set]
        for k in keys_to_remove:
            del elec_conn[k]
            cleaned_elec += 1
    if cleaned_elec:
        print(f"    Cleaned {cleaned_elec} electricity entries")

    # Clean stale SocketPairInvisibleConnector references from socket fragments
    cleaned_socket_refs = 0
    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        frags = value.get("fragmentValues", [])
        for i, frag in enumerate(frags):
            if not isinstance(frag, str) or "CrLogisticsSocketsFragment" not in frag:
                continue
            connectors = re.findall(r'SocketPairInvisibleConnector=\(ID=(\d+)\)', frag)
            if any(int(c) in pole_id_set for c in connectors):
                new_frag = re.sub(r',SocketPairInvisibleConnector=\(ID=\d+\)', '', frag)
                if new_frag != frag:
                    frags[i] = new_frag
                    cleaned_socket_refs += 1
    if cleaned_socket_refs:
        print(f"    Cleaned {cleaned_socket_refs} stale socket connector refs")

    print(f"\n  Revert complete!")
    return {"poles_removed": removed, "rewrites": ok_count}


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fix ALL junctions (universal v5)")
    parser.add_argument("save_file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revert", action="store_true", help="Revert a previous fix (remove invisible poles)")
    parser.add_argument("--json", action="store_true", help="Also write a .sav.json file")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    sav_path = Path(args.save_file)
    print("=" * 65)
    if args.revert:
        print("  StarRupture Junction Fix — REVERT MODE")
        print("  Remove invisible poles and restore spline endpoints")
    else:
        print("  StarRupture Junction Fix — Direct Socket Fix")
        print("  Writes CrLogisticsSocketsFragment to merger entities")
    print("=" * 65)
    print(f"  Mode: {'APPLY' if args.apply else 'DRY RUN'}\n")

    data = read_sav(sav_path)

    # ── Revert mode ──
    if args.revert:
        result = revert_fix(data, apply=args.apply, verbose=args.verbose)
        if args.apply and result and result["poles_removed"] > 0:
            backup_path = sav_path.with_suffix(".sav.backup")
            i = 0
            while backup_path.exists():
                i += 1
                backup_path = sav_path.with_suffix(f".sav.backup{i}")
            shutil.copy2(sav_path, backup_path)
            print(f"\n  Backup: {backup_path}")
            write_sav(sav_path, data)
        if args.json:
            json_path = sav_path.with_suffix(".sav.json")
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n  JSON export: {json_path}")
        return

    entities = find_entity_container(data)
    if not entities:
        print("  ERROR: No entity container found")
        return

    # ── Dangling reference cleanup ──
    all_eids = set()
    for key in entities:
        eid = extract_id(key)
        if eid is not None:
            all_eids.add(eid)

    broken_spline_keys = []
    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        for frag in value.get("fragmentValues", []):
            if not isinstance(frag, str) or "AuSplineConnectionFragment" not in frag:
                continue
            s = re.search(r'StartEntity=\(ID=(\d+)\)', frag)
            e = re.search(r'EndEntity=\(ID=(\d+)\)', frag)
            if s and e:
                if int(s.group(1)) not in all_eids or int(e.group(1)) not in all_eids:
                    broken_spline_keys.append(key)
                    break

    if broken_spline_keys:
        print(f"  Dangling splines found: {len(broken_spline_keys)}")

        removed_ids = set()
        for key in broken_spline_keys:
            eid = extract_id(key)
            if eid is not None:
                removed_ids.add(eid)

        if args.apply:
            for key in broken_spline_keys:
                eid = extract_id(key)
                if eid is not None:
                    all_eids.discard(eid)
                del entities[key]

            # Clean up intersection fragments referencing removed entities
            cleaned_intersections = 0
            for key, value in entities.items():
                if not isinstance(value, dict):
                    continue
                frags = value.get("fragmentValues", [])
                for i, frag in enumerate(frags):
                    if not isinstance(frag, str) or "CrLogisticsIntersectionFragment" not in frag:
                        continue
                    refs = [int(m.group(1)) for m in re.finditer(r'Entity=\(ID=(\d+)\)', frag)]
                    if any(r in removed_ids for r in refs):
                        frags[i] = "/Script/Chimera.CrLogisticsIntersectionFragment(CachedMoveSpeedPerLine=())"
                        cleaned_intersections += 1

            # Clean up electricity connectorData
            cleaned_elec = 0
            elec_conn = data.get("itemData", {}).get("Mass", {}).get("electricitySubsystemState", {}).get("connectorData", {})
            if isinstance(elec_conn, dict):
                keys_to_remove = [k for k in elec_conn if extract_id(k) in removed_ids]
                for k in keys_to_remove:
                    del elec_conn[k]
                    cleaned_elec += 1

            print(f"    ✓ Removed {len(broken_spline_keys)} broken splines")
            print(f"    ✓ Cleaned {cleaned_intersections} intersection fragments")
            print(f"    ✓ Cleaned {cleaned_elec} electricity entries")
        else:
            print(f"    (will be removed on --apply)")

    # ── Discovery ──
    junction_ids = {}  # eid -> type string
    splines = []       # list of spline info dicts
    pole_keys = []

    for key, value in entities.items():
        eid = extract_id(key)
        if eid is None or not isinstance(value, dict):
            continue
        config = get_config(value)

        if "DroneLane_3" in config:
            junction_ids[eid] = "3-way"
        elif "DroneLane_5" in config:
            junction_ids[eid] = "5-way"
        elif "DroneMerger_3To1" in config:
            junction_ids[eid] = "merger-3"
        elif "DroneMerger_5To1" in config:
            junction_ids[eid] = "merger-5"
        elif "DA_DroneJunction_4" in config:
            junction_ids[eid] = "4-way"
        if "DroneInvisiblePole" in config:
            pole_keys.append(key)

        sd = get_spline_data(value)
        if sd:
            sd["id"] = eid
            sd["key"] = key
            sd["entity"] = value
            splines.append(sd)

    junction_set = set(junction_ids.keys())
    print(f"  Junctions: {len(junction_ids)}")
    print(f"  Splines: {len(splines)}")

    if pole_keys:
        print(f"  WARNING: {len(pole_keys)} invisible poles detected!")
        print(f"           Run with --revert first to clean them up.")

    # ── Build per-junction spline index ──
    junction_touches = defaultdict(list)

    for sp in splines:
        if sp["start_id"] in junction_set:
            junction_touches[sp["start_id"]].append({
                "spline": sp,
                "field": "Start",
                "neighbor": sp["end_id"],
                "pos": sp["start_pos"],
            })
        if sp["end_id"] in junction_set:
            junction_touches[sp["end_id"]].append({
                "spline": sp,
                "field": "End",
                "neighbor": sp["start_id"],
                "pos": sp["end_pos"],
            })

    # ── Process each junction: write socket data ──
    next_entity_id = get_max_entity_id(entities) + 1
    socket_writes = []  # (jid, entity, fragment_str, num_sockets)
    new_poles = []       # pole IDs to create
    junctions_fixed = 0
    lane3_fixed = 0
    junctions_skipped = 0
    already_has_sockets = 0

    for jid in sorted(junction_ids.keys()):
        touches = junction_touches.get(jid, [])
        if not touches:
            junctions_skipped += 1
            continue

        junction_entity = entities.get(f"(ID={jid})")
        if not junction_entity:
            junctions_skipped += 1
            continue

        jtype = junction_ids[jid]

        # ── DroneLane_3: needs invisible pole pairing ──
        if jtype == "3-way":
            # Check if already has SocketPairInvisibleConnector
            existing_frag = None
            for f in junction_entity.get("fragmentValues", []):
                if isinstance(f, str) and "CrLogisticsSocketsFragment" in f:
                    existing_frag = f
                    break
            if existing_frag and "SocketPairInvisibleConnector" in existing_frag:
                already_has_sockets += 1
                junctions_skipped += 1
                continue

            # Get entity transform
            transform = junction_entity.get("spawnData", {}).get("transform", {})
            pos = transform.get("translation", {})
            rot = transform.get("rotation", {})
            ex = pos.get("x", 0)
            ey = pos.get("y", 0)
            ez = pos.get("z", 0)
            qx = rot.get("x", 0)
            qy = rot.get("y", 0)
            qz = rot.get("z", 0)
            qw = rot.get("w", 1)

            # Allocate 3 pole IDs (one per lane)
            pole_ids = [next_entity_id, next_entity_id + 1, next_entity_id + 2]
            next_entity_id += 3

            # Build 6 socket positions (3 lanes × 2 sides)
            socket_positions = []
            for lane_idx, (offset_a, offset_b) in enumerate(DRONE_LANE_3_OFFSETS):
                pid = pole_ids[lane_idx]

                # Rotate offsets by entity quaternion
                ra = rotate_by_quat(offset_a, (qx, qy, qz, qw))
                rb = rotate_by_quat(offset_b, (qx, qy, qz, qw))

                # World positions
                wa = (ex + ra[0], ey + ra[1], ez + ra[2])
                wb = (ex + rb[0], ey + rb[1], ez + rb[2])

                # Determine ConnectionType by matching spline endpoints
                ctype_a = match_spline_to_socket(wa, touches)
                ctype_b = match_spline_to_socket(wb, touches)

                socket_positions.append((wa[0], wa[1], wa[2], ctype_a, pid))
                socket_positions.append((wb[0], wb[1], wb[2], ctype_b, pid))

            frag_str = build_socket_fragment(socket_positions)
            new_poles.extend(pole_ids)
            socket_writes.append((jid, junction_entity, frag_str, 6))
            junctions_fixed += 1
            lane3_fixed += 1

            if args.verbose:
                print(f"\n  DroneLane_3 {jid}: 6 sockets, 3 poles ({pole_ids})")
                for i in range(0, len(socket_positions), 2):
                    sa, sb = socket_positions[i], socket_positions[i+1]
                    print(f"    Lane {i//2}: A({sa[3] or '-'}) B({sb[3] or '-'}) pole={sa[4]}")

        # ── Other junction types: basic socket fragment ──
        else:
            # Skip if already has socket fragment
            existing_socket = any(
                isinstance(f, str) and "CrLogisticsSocketsFragment" in f
                for f in junction_entity.get("fragmentValues", [])
            )
            if existing_socket:
                already_has_sockets += 1
                junctions_skipped += 1
                continue

            # Collect socket positions from spline endpoints
            socket_positions = []
            seen_positions = set()
            for t in touches:
                if not t["pos"]:
                    continue
                pos_key = (round(t["pos"][0], 1), round(t["pos"][1], 1), round(t["pos"][2], 1))
                if pos_key in seen_positions:
                    continue
                seen_positions.add(pos_key)
                ctype = "Output" if t["field"] == "Start" else "Input"
                socket_positions.append((t["pos"][0], t["pos"][1], t["pos"][2], ctype))

            if not socket_positions:
                junctions_skipped += 1
                continue

            frag_str = build_socket_fragment(socket_positions)
            socket_writes.append((jid, junction_entity, frag_str, len(socket_positions)))
            junctions_fixed += 1

            if args.verbose:
                print(f"\n  Junction {jid} ({jtype}): {len(socket_positions)} sockets")
                for sp in socket_positions:
                    print(f"    {sp[3]}: ({sp[0]:.1f}, {sp[1]:.1f}, {sp[2]:.1f})")

    # ── Summary ──
    print(f"\n{'='*65}")
    print(f"  SUMMARY")
    print(f"{'='*65}")
    print(f"  Junctions to fix: {junctions_fixed}")
    if lane3_fixed:
        print(f"    DroneLane_3 (with pole pairing): {lane3_fixed}")
        print(f"    Other junction types: {junctions_fixed - lane3_fixed}")
    print(f"  Invisible poles to create: {len(new_poles)}")
    print(f"  Junctions skipped: {junctions_skipped}")
    if already_has_sockets:
        print(f"  Already have socket data: {already_has_sockets}")

    if not args.apply:
        print(f"\n  DRY RUN. Use --apply to modify the save.")
    else:
        # ── Apply ──
        print(f"\n  Applying...")

        # Create invisible pole entities
        for pid in new_poles:
            entities[f"(ID={pid})"] = copy.deepcopy(INVISIBLE_POLE_TEMPLATE)
        if new_poles:
            print(f"    Created {len(new_poles)} invisible pole entities")

        # Write socket fragments
        for jid, entity, frag_str, num_sockets in socket_writes:
            action = write_socket_fragment(entity, frag_str)
            if args.verbose:
                print(f"    Junction {jid}: {action} ({num_sockets} sockets)")
        print(f"    Written {len(socket_writes)} socket fragments")

        # ── Backup and write ──
        backup_path = sav_path.with_suffix(".sav.backup")
        i = 0
        while backup_path.exists():
            i += 1
            backup_path = sav_path.with_suffix(f".sav.backup{i}")
        shutil.copy2(sav_path, backup_path)
        print(f"\n  Backup: {backup_path}")

        write_sav(sav_path, data)

        print(f"\n  Done! {junctions_fixed} junctions fixed")
        print(f"  With SocketSaveFix mod: permanent fix")
        print(f"  Without mod: fix lasts until next save/load")

    # ── JSON export (works in both dry-run and apply modes) ──
    if args.json:
        json_path = sav_path.with_suffix(".sav.json")
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  JSON export: {json_path}")


if __name__ == "__main__":
    main()
