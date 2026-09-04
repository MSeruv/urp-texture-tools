#!/usr/bin/env python3
"""
Convert downloaded PBR texture sets (metallic/roughness workflow) into
Unity URP *Specular* setup maps, renamed to the T_MATERIALTYPE_ID convention.

    python unity_urp_convert.py                 # convert everything new
    python unity_urp_convert.py --force         # redo everything
    python unity_urp_convert.py --dry-run       # show what would happen
    python unity_urp_convert.py --only metal_plate --name T_METAL_PLATE

Outputs land in <root>/Unity_URP/<T_NAME>/ and are skipped when they are
already newer than every source file they were built from.

Produced maps (URP/Lit, Workflow Mode = Specular):
    T_X_Y_BaseColor.png   sRGB  RGB = diffuse (albedo * (1 - metallic))
    T_X_Y_Specular.png    sRGB  RGB = specular colour, A = smoothness
    T_X_Y_Normal.png            OpenGL / Y+ convention
    T_X_Y_Height.png            linear, single channel
    T_X_Y_Occlusion.png         linear, single channel   (if the set has one)
    T_X_Y_Emissive.png    sRGB                           (if the set has one)
"""

import argparse
import json
import os
import re
import sys

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2
import numpy as np

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

DEFAULT_ROOT = r"d:\Unity\Downloads"
OUT_DIRNAME = "Unity_URP"

# Dielectric reflectance at normal incidence, linear. 0.04 == Unity's default.
DIELECTRIC_F0 = 0.04

SUFFIX = {
    "basecolor": "_BaseColor",
    "specular": "_Specular",
    "normal": "_Normal",
    "height": "_Height",
    "occlusion": "_Occlusion",
    "emissive": "_Emissive",
}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr", ".bmp", ".hdr"}

# filename token -> logical channel. Order matters: the first match wins.
MAP_PATTERNS = [
    ("normal_gl", r"(nor(mal)?[_\-]?gl|normalopengl)"),
    ("normal_dx", r"(nor(mal)?[_\-]?dx|normaldirectx)"),
    ("normal", r"(normal|_nor(_|\d|$)|nrm|_n(_|\d|$))"),
    ("metallic", r"(metall?ic|metalness|_metal(_|\d|$)|_mtl(_|\d|$))"),
    ("roughness", r"(rough(ness)?|_rgh(_|\d|$))"),
    ("glossiness", r"(gloss(iness)?|smoothness)"),
    ("occlusion", r"(ambientocclusion|occlusion|_ao(_|\d|$)|_ao$)"),
    ("height", r"(displacement|height|_disp(_|\d|$)|_hgt(_|\d|$)|bump)"),
    ("emissive", r"(emissi(ve|on))"),
    ("opacity", r"(opacity|alpha|transparen)"),
    ("basecolor", r"(basecolor|base_color|albedo|diffuse|_diff(_|\d|$)|_col(_|\d|$)|color|_alb(_|\d|$))"),
]

# tokens dropped when deriving a material name from a folder name
NOISE_TOKENS = re.compile(
    r"^(\d+k|\d+px|jpg|jpeg|png|exr|tga|tiff?|blend|zip|ue|sbsar|pbr|"
    r"texture|textures|material|materials|res)$",
    re.I,
)


# --------------------------------------------------------------------------
# colour helpers (image data is handled as float32 in 0..1 throughout)
# --------------------------------------------------------------------------

def srgb_to_linear(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * (x ** (1 / 2.4)) - 0.055)


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------

def read_exr(path):
    """Read an OpenEXR file as an RGB(A) or grey float32 array.

    Used because most OpenCV wheels are built without EXR support.
    """
    try:
        import OpenEXR
    except ImportError:
        raise IOError(
            "cannot read %s -- OpenCV has no EXR support in this build.\n"
            "Install a reader with:  pip install OpenEXR" % path)

    with OpenEXR.File(path) as exr:
        channels = exr.channels()
        for name in ("RGBA", "RGB", "Y"):
            if name in channels:
                return np.asarray(channels[name].pixels, dtype=np.float32)
        planes = {k.upper(): np.asarray(v.pixels, dtype=np.float32)
                  for k, v in channels.items()}
        for group in (("R", "G", "B", "A"), ("R", "G", "B")):
            if all(c in planes for c in group):
                return np.stack([planes[c] for c in group], axis=2)
        return next(iter(planes.values()))


def imread(path):
    """Read any supported image as float32 0..1.

    Returns (array, source_bit_depth). Array is HxW (grey), HxWx3 or HxWx4,
    channel order RGB(A).
    """
    if os.path.splitext(path)[1].lower() == ".exr":
        img = read_exr(path)
        if img.ndim == 3 and img.shape[2] == 1:
            img = img[:, :, 0]
        return np.ascontiguousarray(img), 16

    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError("could not decode " + path)

    if img.dtype == np.uint8:
        bits, img = 8, img.astype(np.float32) / 255.0
    elif img.dtype == np.uint16:
        bits, img = 16, img.astype(np.float32) / 65535.0
    else:  # float32 from exr/hdr -- already linear values
        bits, img = 16, img.astype(np.float32)

    if img.ndim == 3:  # cv2 hands back BGR / BGRA
        if img.shape[2] == 1:
            img = img[:, :, 0]
        elif img.shape[2] == 3:
            img = img[:, :, ::-1]
        elif img.shape[2] == 4:
            img = np.concatenate([img[:, :, 2::-1], img[:, :, 3:4]], axis=2)
    return np.ascontiguousarray(img), bits


def imwrite(path, img, bits=8):
    """Write float32 0..1 RGB / RGBA / grey data as a PNG."""
    img = np.clip(img, 0.0, 1.0)
    if bits == 16:
        out = (img * 65535.0 + 0.5).astype(np.uint16)
    else:
        out = (img * 255.0 + 0.5).astype(np.uint8)

    if out.ndim == 3:
        if out.shape[2] == 3:
            out = out[:, :, ::-1]
        elif out.shape[2] == 4:
            out = np.concatenate([out[:, :, 2::-1], out[:, :, 3:4]], axis=2)

    ok, buf = cv2.imencode(".png", out, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise IOError("could not encode " + path)
    buf.tofile(path)


def to_grey(img):
    return img if img.ndim == 2 else img[:, :, :3].mean(axis=2)


def to_rgb(img):
    if img.ndim == 2:
        return np.repeat(img[:, :, None], 3, axis=2)
    return img[:, :, :3]


def fit(img, shape):
    """Resample to (h, w) if the map does not match the reference resolution."""
    if img.shape[:2] == shape:
        return img
    h, w = shape
    interp = cv2.INTER_AREA if img.shape[0] > h else cv2.INTER_CUBIC
    return cv2.resize(img, (w, h), interpolation=interp)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def classify(filename):
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    for key, pattern in MAP_PATTERNS:
        if re.search(pattern, stem):
            return key
    return None


def find_sets(root, out_root):
    """Group texture files into material sets, keyed by their owning folder."""
    sets = {}
    for dirpath, dirnames, filenames in os.walk(root):
        abs_dir = os.path.abspath(dirpath)
        if abs_dir == out_root or abs_dir.startswith(out_root + os.sep):
            dirnames[:] = []
            continue

        maps = {}
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXT:
                continue
            key = classify(fn)
            if key and key not in maps:
                maps[key] = os.path.join(dirpath, fn)

        if not maps:
            continue
        if not maps.keys() & {"basecolor", "normal", "normal_gl", "normal_dx"}:
            continue  # a lone preview/thumbnail image, not a material set

        # a "textures" subfolder belongs to its parent material folder
        owner = dirpath
        if os.path.basename(dirpath).lower() in ("textures", "texture", "maps") \
                and abs_dir != os.path.abspath(root):
            owner = os.path.dirname(dirpath)

        bucket = sets.setdefault(owner, {})
        for key, path in maps.items():
            bucket.setdefault(key, path)
    return sets


def derive_name(folder):
    """'Metal021_2K-JPG' -> T_METAL_021 ; 'metal_plate_02_4k.blend' -> T_METAL_PLATE_02"""
    raw = os.path.basename(os.path.normpath(folder))
    raw = re.sub(r"\.(blend|zip|fbx|obj|sbsar)$", "", raw, flags=re.I)

    tokens = []
    for part in re.split(r"[\s_\-.]+", raw):
        if not part or NOISE_TOKENS.match(part):
            continue
        for piece in re.findall(r"\d+|[A-Za-z]+", part):  # "Metal021" -> "Metal", "021"
            if NOISE_TOKENS.match(piece):
                continue
            tokens.append(piece.upper())
    return "T_" + "_".join(tokens or ["MATERIAL"])


def needs_rebuild(sources, outputs, force):
    if force:
        return True
    if not outputs or not all(os.path.exists(p) for p in outputs):
        return True
    return max(os.path.getmtime(p) for p in sources) > \
        min(os.path.getmtime(p) for p in outputs)


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------

def convert(folder, maps, name, out_dir, dry_run=False, force=False):
    planned = {}
    if "basecolor" in maps:
        planned["basecolor"] = os.path.join(out_dir, name + SUFFIX["basecolor"] + ".png")
        planned["specular"] = os.path.join(out_dir, name + SUFFIX["specular"] + ".png")
    if maps.keys() & {"normal_gl", "normal_dx", "normal"}:
        planned["normal"] = os.path.join(out_dir, name + SUFFIX["normal"] + ".png")
    for key in ("height", "occlusion", "emissive"):
        if key in maps:
            planned[key] = os.path.join(out_dir, name + SUFFIX[key] + ".png")

    notes_path = os.path.join(out_dir, name + "_ImportNotes.txt")
    outputs = list(planned.values()) + [notes_path]

    if not needs_rebuild(list(maps.values()), outputs, force):
        print("  skip   %s  (up to date)" % name)
        return "skipped", {}

    print("  build  %s" % name)
    for key in sorted(maps):
        print("           %-11s <- %s" % (key, os.path.basename(maps[key])))
    if dry_run:
        for path in planned.values():
            print("           would write %s" % os.path.basename(path))
        return "dry-run", {}

    os.makedirs(out_dir, exist_ok=True)
    written = {}

    # the colour map sets the output resolution; every other map is fitted to it
    ref_src = maps.get("basecolor") or maps.get("normal_gl") or \
        maps.get("normal_dx") or maps.get("normal")
    ref_img, _ = imread(ref_src)
    shape = ref_img.shape[:2]

    def load(key, grey=True):
        if key not in maps:
            return None, 8
        img, bits = imread(maps[key])
        return fit(to_grey(img) if grey else to_rgb(img), shape), bits

    # ---- base colour + specular ------------------------------------------
    if "basecolor" in planned:
        albedo, _ = imread(maps["basecolor"])
        albedo_rgb = fit(to_rgb(albedo), shape)
        is_linear_src = os.path.splitext(maps["basecolor"])[1].lower() in (".exr", ".hdr")
        albedo_lin = albedo_rgb if is_linear_src else srgb_to_linear(albedo_rgb)

        metallic, _ = load("metallic")
        if metallic is None:
            metallic = np.zeros(shape, np.float32)
        m = np.clip(metallic, 0.0, 1.0)[:, :, None]

        diffuse_lin = albedo_lin * (1.0 - m)
        specular_lin = DIELECTRIC_F0 * (1.0 - m) + albedo_lin * m

        rough, _ = load("roughness")
        if rough is not None:
            smoothness = 1.0 - rough
        elif "glossiness" in maps:
            smoothness, _ = load("glossiness")
        else:
            smoothness = np.full(shape, 0.5, np.float32)

        imwrite(planned["basecolor"], linear_to_srgb(diffuse_lin), 8)
        spec = np.concatenate(
            [linear_to_srgb(specular_lin), np.clip(smoothness, 0.0, 1.0)[:, :, None]], axis=2)
        imwrite(planned["specular"], spec, 8)
        written["basecolor"] = planned["basecolor"]
        written["specular"] = planned["specular"]

    # ---- normal (Unity expects the OpenGL / Y+ convention) ----------------
    if "normal" in planned:
        if "normal_gl" in maps:
            src, flip_g = maps["normal_gl"], False
        elif "normal" in maps:
            src, flip_g = maps["normal"], False
        else:
            src, flip_g = maps["normal_dx"], True
        nrm, bits = imread(src)
        nrm = fit(to_rgb(nrm), shape).copy()
        if flip_g:
            nrm[:, :, 1] = 1.0 - nrm[:, :, 1]
        imwrite(planned["normal"], nrm, 16 if bits == 16 else 8)
        written["normal"] = planned["normal"]

    # ---- single channel data maps ----------------------------------------
    for key in ("height", "occlusion"):
        if key in planned:
            img, bits = load(key)
            imwrite(planned[key], img, 16 if (bits == 16 and key == "height") else 8)
            written[key] = planned[key]

    if "emissive" in planned:
        img, _ = load("emissive", grey=False)
        imwrite(planned["emissive"], img, 8)
        written["emissive"] = planned["emissive"]

    write_notes(notes_path, name, folder, maps, written)
    return "built", written


def write_notes(path, name, folder, maps, written):
    lines = [
        "%s  --  Unity URP Lit, Workflow Mode = Specular" % name,
        "source: %s" % folder,
        "",
        "Material setup (URP/Lit):",
        "  Workflow Mode ......... Specular",
        "  Smoothness Source ..... Specular Alpha",
        "",
        "Texture import settings:",
    ]
    if "basecolor" in written:
        lines += [
            "  " + os.path.basename(written["basecolor"]),
            "      -> Base Map      | sRGB ON",
            "  " + os.path.basename(written["specular"]),
            "      -> Specular Map  | sRGB ON | Alpha Source: Input Texture Alpha",
            "      -> compression must preserve alpha (BC7 / DXT5), not DXT1",
        ]
    if "normal" in written:
        lines += [
            "  " + os.path.basename(written["normal"]),
            "      -> Normal Map    | Texture Type: Normal map (already OpenGL / Y+)",
        ]
    if "height" in written:
        lines += ["  " + os.path.basename(written["height"]),
                  "      -> Height Map    | sRGB OFF"]
    if "occlusion" in written:
        lines += ["  " + os.path.basename(written["occlusion"]),
                  "      -> Occlusion Map | sRGB OFF"]
    if "emissive" in written:
        lines += ["  " + os.path.basename(written["emissive"]),
                  "      -> Emission Map  | sRGB ON"]

    lines += [
        "",
        "Conversion applied (in linear space):",
        "  diffuse    = albedo * (1 - metallic)",
        "  specular   = lerp(%s, albedo, metallic)" % DIELECTRIC_F0,
        "  smoothness = 1 - roughness, packed into the specular map alpha",
        "",
        "Source files:",
    ]
    for key in sorted(maps):
        lines.append("  %-12s %s" % (key, os.path.basename(maps[key])))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="folder to scan (default: %(default)s)")
    ap.add_argument("--out", default=None, help="output folder (default: <root>/" + OUT_DIRNAME + ")")
    ap.add_argument("--name", default=None,
                    help="force the material name for a single set, e.g. T_METAL_PLATE")
    ap.add_argument("--only", default=None, help="only process source folders whose name contains this")
    ap.add_argument("--names", default=None, help="json file mapping source folder name -> T_ name")
    ap.add_argument("--force", action="store_true", help="reconvert even if outputs are up to date")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    out_root = os.path.abspath(args.out or os.path.join(root, OUT_DIRNAME))
    if not os.path.isdir(root):
        sys.exit("root folder not found: " + root)

    overrides = {}
    names_file = args.names or os.path.join(root, "names.json")
    if os.path.exists(names_file):
        with open(names_file, encoding="utf-8") as fh:
            overrides = {k.lower(): v for k, v in json.load(fh).items()}
        print("name overrides: %s" % names_file)

    sets = find_sets(root, out_root)
    if args.only:
        sets = {k: v for k, v in sets.items()
                if args.only.lower() in os.path.basename(os.path.normpath(k)).lower()}
    if not sets:
        sys.exit("no texture sets found")
    if args.name and len(sets) > 1:
        sys.exit("--name needs exactly one set, found %d (narrow it with --only)" % len(sets))

    print("scanning %s\noutput   %s\n" % (root, out_root))
    tally = {}
    for folder in sorted(sets):
        key = os.path.basename(os.path.normpath(folder)).lower()
        name = args.name or overrides.get(key) or derive_name(folder)
        name = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
        if not name.startswith("T_"):
            name = "T_" + name
        status, _ = convert(folder, sets[folder], name,
                            os.path.join(out_root, name), args.dry_run, args.force)
        tally[status] = tally.get(status, 0) + 1

    print("\n" + ", ".join("%d %s" % (v, k) for k, v in tally.items()))


if __name__ == "__main__":
    main()
