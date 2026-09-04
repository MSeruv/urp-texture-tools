# URP Texture Tools

Two small Python tools for getting downloaded PBR textures into Unity URP using the
**Specular** workflow instead of Metallic.

Most texture sites (AmbientCG, Poly Haven, Quixel and so on) ship metallic/roughness
maps. Unity's URP/Lit shader can run in Specular mode, but it wants different inputs:
a diffuse colour with the metal contribution removed, a specular colour map, and
smoothness packed into the specular alpha. Doing that by hand in Photoshop for every
material gets old fast, so these scripts do it.

## What's in here

| Script | What it does |
| --- | --- |
| `unity_urp_convert.py` | Batch converts downloaded texture folders to URP Specular maps and renames them to `T_TYPE_ID` |
| `urp_rename_gui.py` | A small Tkinter window for renaming an already converted set to something else |

## Requirements

Python 3.9 or newer.

```
pip install opencv-python numpy
pip install OpenEXR          # only if your textures include .exr files
```

Most OpenCV wheels are built without EXR support, which is why the `OpenEXR` package
is listed separately. Poly Haven ships `.exr` maps, AmbientCG does not. The converter
tells you if it hits an EXR it cannot read.

The GUI needs nothing beyond the standard library.

## The converter

```
python unity_urp_convert.py                      # convert anything new
python unity_urp_convert.py --dry-run            # show what it would do
python unity_urp_convert.py --force              # rebuild everything
python unity_urp_convert.py --root "d:\Textures" # scan somewhere else
```

It walks the root folder, groups image files into material sets by filename, and writes
the results to `<root>/Unity_URP/<T_NAME>/`. A set is skipped if its outputs are already
newer than every source file, so you can just rerun it after downloading something new.

### The conversion

All of it happens in linear space, not on the sRGB values directly, which matters more
than people expect for the darker metals:

```
diffuse    = albedo * (1 - metallic)
specular   = lerp(0.04, albedo, metallic)
smoothness = 1 - roughness
```

`0.04` is the standard dielectric reflectance at normal incidence, the same value Unity
uses internally. Smoothness gets packed into the alpha of the specular map, which is
what URP/Lit reads by default in Specular mode.

### Output

```
T_METAL_021_BaseColor.png     sRGB, diffuse colour
T_METAL_021_Specular.png      sRGB, RGB = specular colour, A = smoothness
T_METAL_021_Normal.png        OpenGL / Y+ convention
T_METAL_021_Height.png        linear
T_METAL_021_Occlusion.png     linear, only if the source set has one
T_METAL_021_Emissive.png      sRGB, only if the source set has one
T_METAL_021_ImportNotes.txt   the exact Unity importer settings for the above
```

Normal maps are always written in the OpenGL convention because that is what Unity
expects. If a set only ships a DirectX normal, the green channel is flipped
automatically. If it ships both, the OpenGL one is used. 16 bit sources stay 16 bit.

### Naming

Names come from the source folder and follow `T_MATERIALTYPE_ID`. Resolution and format
noise gets stripped:

```
Metal021_2K-JPG          ->  T_METAL_021
metal_plate_02_4k.blend  ->  T_METAL_PLATE_02
```

To override, either pass `--name` for a single set:

```
python unity_urp_convert.py --only metal_plate --name T_METAL_PLATE
```

or drop a `names.json` next to the script mapping source folder names to your own:

```json
{
  "Metal021_2K-JPG": "T_METAL_BRUSHED",
  "metal_plate_02_4k.blend": "T_METAL_PLATE"
}
```

## The renamer GUI

```
python urp_rename_gui.py
python urp_rename_gui.py --root "d:\Unity\Downloads\Unity_URP"
```

Pick a material set, type a material type and a unique ID or variant name, and it
renames the whole set. The `T_` prefix is added for you and is not editable.

It reads the map suffix off each existing file and puts it back afterwards, so a
base colour map stays a base colour map no matter what you call the material. Three
suffix schemes are included: Unity's default (`_BaseColor`), an Albedo style
(`_Albedo`, `_AO`), and a short one (`_BC`, `_N`, `_S`).

Other things it does:

- Live preview of every old name next to its new name, updating as you type
- Refuses to run on a name collision or when the target folder already exists
- Optionally renames the containing folder to match
- Rewrites the `_ImportNotes.txt` contents to point at the new filenames
- Has an undo button, and rolls everything back if a rename fails halfway through

Input is sanitised, so `metal plate!! v2` becomes `METAL_PLATE_V2`.

## Using the results in Unity

Set the material to URP/Lit, then:

- **Workflow Mode** to Specular
- **Smoothness Source** to Specular Alpha

For the textures:

- Base colour and specular: sRGB on
- Specular map: compress with BC7 or DXT5, not DXT1, or you lose the smoothness in the alpha
- Normal: texture type Normal map, already OpenGL so leave it alone
- Height and occlusion: sRGB off

Every converted folder gets an `_ImportNotes.txt` repeating this for that specific set.

## Notes

The converter assumes a metallic/roughness source. If you feed it a set that is already
specular/glossiness it will treat the specular map as an albedo, which is not what you
want. Glossiness maps are picked up when there is no roughness map, so those sets mostly
work, but check the result.

Occlusion and emissive are passed through untouched when present. Neither AmbientCG's
nor Poly Haven's standard sets include an AO map.

## License

Free to use and modify with attribution. You may not resell it. See [LICENSE](LICENSE).
