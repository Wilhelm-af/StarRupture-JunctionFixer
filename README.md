# StarRupture Junction Fixer

**Fix broken 3-way and 5-way drone junctions in StarRupture save files.**

An online tool for maps with way too many broken junctions to fix manually. Drop your save, click fix, done.

**👉 [Use the online tool](https://Wilhelm-af.github.io/StarRupture-JunctionFixer/)**

In need of other usefull tools check out this site: [StarRupture Utilities](https://starrupture-utilities.com/)

---

## The Problem

Drone junctions in StarRupture have been broken since before launch. When you place a 3-way or 5-way junction, the game creates spline connections where all lanes share the same entity IDs for their endpoints. This means the save file stores duplicate/shared references instead of unique ones per lane.

When the game saves your world, it writes these broken shared IDs to disk. On load, the game can't reconstruct which drone belongs to which lane — so **all drones collapse into a single lane** (whichever lane has the duplicated ID). The more you save and load, the worse it gets, with stale poles and drones accumulating each cycle.

The root cause is that `FCrLogisticsSocketsFragment` (which tracks socket-to-pole associations) inherits from `FMassFragment` instead of `FCrMassSavableFragment`, so this data is never persisted to the save file.


An image of broken view
<img width="958" height="539" alt="JunctionFixer_BrokenView" src="https://github.com/user-attachments/assets/2ae1bb78-933c-472c-a4dc-ebea7bab2291" />

And after uploading savefile and downloading the new savefile a working junctions
<img width="958" height="539" alt="JunctionFixer_FixedView" src="https://github.com/user-attachments/assets/346f360a-02ea-479d-ac0a-e97d1db7d6b8" />


## The Fix

This tool rewrites the spline endpoint IDs in your save file so each lane gets its own unique invisible pole entity. Once fixed:

- Each lane is a structurally independent path
- The game's loader can no longer collapse them into one lane
- Drones distribute correctly across all lanes after loading
- **Saving after the fix preserves the correct values** — only newly placed or rebuilt junctions will need fixing again

## How to Use

1. Open the [online tool](https://Wilhelm-af.github.io/StarRupture-JunctionFixer/)
2. Drop your `.sav` file (or click to browse)
3. Click **Dry Run** to preview what will be fixed
4. Click **Fix & Download** to apply and get your fixed save
5. Replace your original save with the fixed one
6. Load the game and verify your junctions

### Save File Location

Your StarRupture saves on windows:
```
%LOCALAPPDATA%\Chimera\Saved\SaveGames\
```
On Linux:
```
/home/USERNAME/.steam/root/userdata/STEAMID/1631270/remote/Saved/SaveGames/
```

### Using the Python Script

There's also a standalone Python script if you prefer command-line:

```bash
# Dry run (preview changes)
python fix_all_junctions.py 0.sav -v

# Apply fix
python fix_all_junctions.py 0.sav --apply
```

## Technical Details

The fix algorithm works per-junction:

1. **Discover** all junction entities (`DroneLane_3` and `DroneLane_5`)
2. **Group** splines touching each junction by their neighbor (other endpoint)
3. **Detect lane axis** — junctions can be rotated, so lanes may separate along X or Y. The tool figures this out automatically by checking which axis has more spread within same-neighbor spline groups
4. **Cluster** all splines at each junction by their position along the lane axis (15-unit tolerance)
5. **Create** one invisible pole entity per lane cluster
6. **Rewrite** spline `StartEntity`/`EndEntity` references from the shared junction ID to the unique pole ID
7. **Clean up** stale poles and drones (the game respawns them correctly on load)

This handles all topologies: simple pairs, chains (A→B→C→D), hubs, partial connections, and mixed 3-way/5-way junctions.

## Privacy

Everything runs client-side in your browser. Your save file is **never uploaded** anywhere — all processing happens locally using JavaScript. The Python script is also fully offline.

## Credits
Made by [@Wilhelm-af](https://github.com/Wilhelm-af/) - Built for the StarRupture community.
Thanks to [UE4SS-RE](https://github.com/UE4SS-RE/RE-UE4SS) for tools for testing and datareading.
And claude for making the python and javascript after problem was discovered. and for more detailed information here.

If you run into issues, feel free to open an issue on this repo.
