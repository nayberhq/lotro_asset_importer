# LOTRO asset importer

Shared planner for biome packs. It reads a catalog, scores free assets, and writes a manifest. It does not build Unreal maps and it does not download Fab listings.

```sh
python biome_assets.py --help
python -m unittest tests/test_biome_assets_standalone.py
```

`lotro_environment` and `lotro_reforge` link this repository as the `asset_importer` submodule. From either parent, `python Scripts/biome_assets.py` runs this file.
