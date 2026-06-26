# factorio-export — producing the prototypes dump

`prototypes_lancedb` is built from Factorio's own **`--dump-data`** export (the
fully-resolved `data.raw` as JSON). You produce that file once per version from a
**vanilla** Factorio install (base + Space Age DLC, **no community mods**), and the
ingest reads it. The per-version dump dirs here are gitignored (each is ~30 MB);
this README is committed so the how-to is reachable in a fresh clone.

## Produce a vanilla dump (PowerShell)

Force a clean base+DLC-only mod set so community mods don't pollute the baseline:

```powershell
$fac    = "C:\path\to\Factorio\bin\x64\factorio.exe"
$moddir = "$env:TEMP\factorio-vanilla-mods"
New-Item -ItemType Directory -Force -Path $moddir | Out-Null
'{"mods":[{"name":"base","enabled":true},{"name":"elevated-rails","enabled":true},{"name":"quality","enabled":true},{"name":"space-age","enabled":true}]}' |
  Set-Content "$moddir\mod-list.json" -Encoding ascii

# Factorio silently no-ops if script-output doesn't exist yet — create it first.
New-Item -ItemType Directory -Force -Path "$env:APPDATA\Factorio\script-output" | Out-Null
& $fac --dump-data --mod-directory $moddir
```

The result lands at `<Factorio write dir>\script-output\data-raw-dump.json`
(`%APPDATA%\Factorio\script-output\` for a Steam install; the install dir for a
portable one). NOTE: the Steam build relaunches via Steam and **detaches**, so poll
for the file's mtime rather than relying on the command's exit.

## Place it for the ingest

```
factorio-export/
└── vanilla_<version>/          # e.g. vanilla_2.1.8/
    ├── data-raw-dump.json      # the dump (copy it here)
    └── version.txt             # the version string, e.g. "2.1.8"
```

Then `make ingest-prototypes` (default path is `factorio-export/vanilla_2.1.8/`), or
point `FACTORIO_DATA_DUMP` at the JSON explicitly. Version is read from the sibling
`version.txt`, else `FACTORIO_VERSION`, else the `vanilla_X.Y.Z` folder name.

## Sanity-check it's vanilla

A real vanilla dump has Space Age present (`quality`/`planet`/`space-location`
types) and **no** `kr-`/`maraxsis-`/other mod-prefixed prototype names, and
`electronic-circuit` is `iron-plate ×1 + copper-cable ×3` (not a modded recipe).
