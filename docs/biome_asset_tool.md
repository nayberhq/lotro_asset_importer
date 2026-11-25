# Standalone Biome Asset Catalog Tool

This is an **independent command-line helper**, not a replacement for the LOTRO terrain or PCG importer. It selects a quality-filtered, role-balanced candidate set for a biome and exports a JSON manifest for the environment generator. It does **not** modify Unreal assets or write to the current world.

## Install / run

Python 3.10+ is sufficient for the normal CLI (standard library only). From this repository root the file is `biome_assets.py`. Checked out as the `asset_importer` submodule, the same file is `asset_importer/biome_assets.py`, and `python Scripts/biome_assets.py` in the parent forwards to it.

```powershell
python biome_assets.py --help
python biome_assets.py catalog sync
python biome_assets.py catalog stats
python biome_assets.py search "temperate forest with oak trees and ferns" --role canopy --top 20 --min-quality 60
python biome_assets.py plan "temperate deciduous forest with oak trees, ferns, moss, worn rocks and fallen logs" --biome forest --min-quality 60 --out generated/forest_assets.json
python biome_assets.py download generated/forest_assets.json --to D:\GameAssets\BiomeAssets
```

`catalog sync` fetches **metadata only** from the public [CodePawl asset-index](https://huggingface.co/datasets/codepawl/asset-index) snapshot; it does not download 3D models or textures. The snapshot covers free/possibly-free assets from several providers including Megascans, ambientCG, Poly Haven, Kenney and Quaternius. It is not every live Fab listing. The database lives in `%USERPROFILE%\.biome-assets\catalog.sqlite3` by default (override with global `--db`).

To smoke-test without internet:

```powershell
python biome_assets.py --db .\tmp-biome-catalog.sqlite3 catalog import examples/biome_assets_sample_catalog.jsonl
python biome_assets.py --db .\tmp-biome-catalog.sqlite3 plan "temperate forest with oak and ferns" --out .\tmp-forest.json
py -m unittest discover -s tests -p 'test_biome_assets_standalone.py' -v
```

The included sample catalog is **synthetic test data only**. Never acquire links from that fixture.

## Deterministic ranking and classification

The selector uses metadata for photoreal style, PBR map completeness, texture resolution, tileability, physical scale, LOD support, source/publisher and ratings **when actually present**. A rating with very few votes is heavily discounted. Neither popularity nor download count is fabricated; the score is a **technical-quality proxy**, not independent proof the asset looks good in Unreal.

Classification is non-agentic by default and separates `canopy`, `understory`, `groundcover`, `rocks`, `debris`, `surface`, `accents`. Role quotas vary by biome; the selector never force-fills with assets below the minimum quality threshold. **Surface** items are material inputs and must never be spawned as 3D foliage. Do not infer that a whole forest pack is one tree; inspect actual meshes and collision after UE import.

Important: `free_candidate` in a dated index is not verified current pricing. Check Fab's current listing and the applicable license before attempting acquisition. Use `--require-ratings` if ratings are important, but many legitimate scanned assets have no public ratings and will then be excluded.

## Optional single Cursor Composer review, cached to SQLite

For subjective appearance/fit checks on shortlisted candidates only:

```powershell
python biome_assets.py plan "temperate forest with oak ferns moss and realistic fallen wood" --candidate-multiplier 2 --out generated/forest_candidates.json
python biome_assets.py review generated/forest_candidates.json --model composer-2.5
python biome_assets.py plan "temperate forest with oak ferns moss and realistic fallen wood" --out generated/forest_assets.json
```

This uses **only the local `agent` executable** in read-only Ask mode (`agent -p --mode=ask --model composer-2.5 --output-format json`). The validated assessments are stored in the SQLite database; ordinary queries **never call an agent**. To review public thumbnails as well, install optional `Pillow` and use:

```powershell
py -m pip install Pillow
python biome_assets.py previews generated/forest_candidates.json --out generated/forest_previews
python biome_assets.py review generated/forest_candidates.json --model composer-2.5 --preview-index generated/forest_previews/index.json
```

A preview sheet is evidence for a human review; do not assume Composer inspected it unless the agent actually opened the file. If Composer is unavailable, deterministic selection continues to work.

## Download rules

Dry-run by default:

```powershell
python biome_assets.py download generated/forest_assets.json --to D:\GameAssets\BiomeAssets
```

Only for direct URLs from **explicitly allowlisted CC0 providers**, optionally enable downloads:

```powershell
python biome_assets.py download generated/forest_assets.json --to D:\GameAssets\BiomeAssets --yes --limit 8
```

Fab content, including Megascans acquired under Fab terms, is **not** downloaded through undocumented Fab endpoints. For such assets, the manifest provides the listing; the user signs into Fab/Epic Launcher, verifies Free pricing, accepts the license, adds the product to the library and downloads/adds it to the project using official UI. The Unreal importer then resolves the downloaded asset path/mesh contents separately. Do not mark manual entries as downloaded until verified on disk.

## Generator integration contract

The JSON manifest contains biome, description, role, stable asset ID, source/listing, license, quality and relevance scores, evidence, and acquisition status. Consumers must:

1. Resolve each asset ID to a verified local file or an actual Unreal `/Game/...` asset path; unresolved candidates cannot spawn.
2. Distinguish source **materials/textures** from physical Static Meshes. `surface` goes to the eight canonical terrain layers; other roles go to PCG only when meshes pass material, dimensions, collision and licensing checks.
3. Recheck mesh physical dimensions, opacity masks and desired scale inside Unreal; catalog information is not proof of correctness.
4. Preserve LOTRO's buildings, paths, terrain, eight canonical ground paint layers and exclusive biome-zone ownership.
5. Use density/spacing/context rules in PCG. A selected set is **not** a directive to spawn every asset or to fill the scene densely.

The tool is deliberately read-only with respect to the existing generator; connect the manifest at your current config-loading boundary when you are ready, rather than introducing a second PCG pipeline.

## Known boundaries

- No live, complete Fab search API or invisible sign-in workaround is implemented.
- Database metadata is a snapshot; free pricing, licensing, asset availability, and true popularity may change.
- Automated code and offline tests can verify parser/scorer behavior; validating actual UE 5.8 material/mesh quality requires Unreal import and screenshots.
- Only the `previews` subcommand requires optional Pillow.
