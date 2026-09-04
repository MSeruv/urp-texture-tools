# Unity Texture Tools

One Python tool for getting downloaded PBR textures into Unity, for **URP** (Specular or
Metallic workflow) and **HDRP** (packed mask map).

Most texture sites (AmbientCG, Poly Haven, Quixel and so on) ship metallic/roughness
maps. None of the three Unity setups want exactly those files: URP in Specular mode
needs a diffuse with the metal contribution removed plus a specular colour map, URP in
Metallic mode wants smoothness in the metallic map's alpha, and HDRP wants metallic,
occlusion and smoothness packed into a single mask map. Doing that by hand in Photoshop
for every material gets old fast, so this does it.

```
python unity_texture_tools.py
```

## The workspace

On first run it creates a workspace, by default under `d:\Unity\Downloads`:

```
<workspace>/
    Queue/          drop your downloaded texture folders (or .zip) in here
    Converted/      results land here, one folder per material
```

Point it somewhere else with `--workspace "d:\Textures"`, or with Browse in the window.
Nothing in `Queue/` is ever modified or deleted.

## Requirements

Python 3.9 or newer.

```
pip install opencv-python numpy
pip install OpenEXR          # only if your textures include .exr files
```

Most OpenCV wheels are built without EXR support, which is why `OpenEXR` is listed
separately. Poly Haven ships `.exr` maps, AmbientCG does not. The converter tells you if
it hits an EXR it cannot read. The Rename tab needs nothing beyond the standard library
and still works if OpenCV is missing.

## The Convert tab

Pick a pipeline, hit **Scan queue**, and every material set found in the queue is listed
with the maps it has and the name it will get. Zips are unpacked first if the box is
ticked. Then **Convert all**, or select rows and **Convert selected**.

Sets are grouped by folder from the filenames, so an unpacked `Metal021_2K-JPG` folder
or a nest of `textures/` subfolders both work. A set whose outputs are already newer
than every source file is skipped, so you can rerun after downloading something new.
Tick **Rebuild even if up to date** to redo them anyway, or **Dry run** to see what
would happen without writing anything.

### Naming

Each set gets an auto derived `T_MATERIALTYPE_ID` name with resolution and format noise
stripped:

```
Metal021_2K-JPG          ->  T_METAL_021
metal_plate_02_4k.blend  ->  T_METAL_PLATE_02
Foliage 03 (2K)          ->  T_FOLIAGE_03
```

**Double click the name to change it.** Whatever you type is sanitised and gets the `T_`
prefix, so `metal brushed!! v2` becomes `T_METAL_BRUSHED_V2`. **Reset names** puts the
defaults back. To set names up front instead, drop a `names.json` in the workspace root
mapping queue folder names to your own:

```json
{
  "Metal021_2K-JPG": "T_METAL_BRUSHED",
  "metal_plate_02_4k.blend": "T_METAL_PLATE"
}
```

### The three pipelines

All of the maths happens in linear space, not on the sRGB values directly, which matters
more than people expect for the darker metals.

**URP/Lit, Specular workflow**

```
diffuse    = albedo * (1 - metallic)
specular   = lerp(0.04, albedo, metallic)
smoothness = 1 - roughness, packed into the specular alpha
```

```
T_METAL_021_BaseColor.png     sRGB, diffuse colour
T_METAL_021_Specular.png      sRGB, RGB = specular colour, A = smoothness
T_METAL_021_Normal.png        OpenGL / Y+
T_METAL_021_Height.png        linear
T_METAL_021_Occlusion.png     linear, only if the source set has one
T_METAL_021_Emissive.png      sRGB, only if the source set has one
T_METAL_021_ImportNotes.txt   the exact importer settings for the above
```

`0.04` is the standard dielectric reflectance at normal incidence, the same value Unity
uses internally.

**URP/Lit, Metallic workflow** — base colour passes through untouched and the surface
data goes into one map:

```
T_METAL_021_BaseColor.png             sRGB
T_METAL_021_MetallicSmoothness.png    linear, RGB = metallic, A = smoothness
```

plus the same normal, height, occlusion and emissive.

**HDRP/Lit** — occlusion is packed in rather than being a separate map:

```
T_METAL_021_BaseColor.png     sRGB
T_METAL_021_MaskMap.png       linear, R = metallic, G = occlusion, B = detail, A = smoothness
T_METAL_021_Normal.png        OpenGL / Y+
T_METAL_021_Height.png        linear
T_METAL_021_Emissive.png      sRGB
```

Missing channels fall back to Unity's own defaults: no metallic map means 0, no occlusion
means 1, no roughness or glossiness means 0.5 smoothness.

### Details worth knowing

Normal maps are always written in the OpenGL convention because that is what Unity
expects. If a set only ships a DirectX normal the green channel is flipped
automatically; if it ships both, the OpenGL one is used. 16 bit sources stay 16 bit.

The colour map sets the output resolution and every other map is resampled to it, so
mixed resolution sets come out consistent.

An opacity map, or real alpha in the albedo, is packed into the base colour alpha.

Converting one set into two different pipelines writes into the same folder, so the
first pipeline's maps stay behind next to the second one's. That is reported in the log
after the build, and left alone rather than deleted.

## The Rename tab

Pick a converted set, type a material type and a unique ID or variant, and it renames
the whole set. The `T_` prefix is added for you and is not editable.

It reads the map suffix off each existing file and puts it back afterwards, so a base
colour map stays a base colour map no matter what you call the material. Three suffix
schemes are included: Unity's default (`_BaseColor`), an Albedo style (`_Albedo`, `_AO`),
and a short one (`_BC`, `_N`, `_S`).

Other things it does:

- Live preview of every old name next to its new name, updating as you type
- Refuses to run on a name collision or when the target folder already exists
- Optionally renames the containing folder to match
- Rewrites the `_ImportNotes.txt` to point at the new filenames, leaving its record of
  the original source files intact
- Has an undo button that restores the notes text as well as the filenames, and rolls
  everything back if a rename fails halfway through

## Command line

The UI is the point, but the conversion runs headless too:

```
python unity_texture_tools.py --cli                                  # urp specular
python unity_texture_tools.py --cli --pipeline hdrp --extract
python unity_texture_tools.py --cli --pipeline urp-metallic --force
python unity_texture_tools.py --cli --dry-run
python unity_texture_tools.py --cli --only metal_plate --name T_METAL_PLATE
```

`--workspace`, `--names`, `--only`, `--name`, `--extract`, `--force` and `--dry-run` all
behave the way their UI equivalents do.

## Using the results in Unity

**URP/Lit, Specular** — Workflow Mode to Specular, Smoothness Source to Specular Alpha.

**URP/Lit, Metallic** — Workflow Mode to Metallic, Smoothness Source to Metallic Alpha.

**HDRP/Lit** — Material Type Standard, drop the mask map in the Mask Map slot. There is
no separate occlusion slot, it comes from the mask.

For the textures:

- Base colour, specular and emissive: sRGB on
- Metallic, mask, height and occlusion maps: sRGB off
- Specular, metallic and mask maps: compress with BC7 or DXT5, not DXT1, or you lose the
  smoothness stored in the alpha
- Normal: texture type Normal map, already OpenGL so leave it alone

Every converted folder gets an `_ImportNotes.txt` repeating this for that specific set
and pipeline.

## Notes

The converter assumes a metallic/roughness source. If you feed it a set that is already
specular/glossiness it will treat the specular map as an albedo, which is not what you
want. Glossiness maps are picked up when there is no roughness map, so those sets mostly
work, but check the result.

Occlusion and emissive are passed through untouched when present. Neither AmbientCG's
nor Poly Haven's standard sets include an AO map.

## License

Free to use and modify with attribution. You may not resell it. See [LICENSE](LICENSE).
