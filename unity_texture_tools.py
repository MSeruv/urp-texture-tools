#!/usr/bin/env python3
"""
Unity Texture Tools -- one window for converting and renaming PBR texture sets.

On first run a workspace is created wherever you point it:

    <workspace>/Queue/        drop downloaded texture folders (or .zip) here
    <workspace>/Converted/    Unity ready sets land here, one folder per material

The Convert tab scans the queue, groups image files into material sets, offers
an auto derived T_TYPE_ID name for each one (editable, double click it) and
writes the maps the chosen pipeline needs. The Rename tab renames an already
converted set to something else without losing track of which map is which.

    python unity_texture_tools.py
    python unity_texture_tools.py --workspace "d:\\Textures"
    python unity_texture_tools.py --cli --pipeline hdrp --force

Pipelines:
    urp-specular   URP/Lit, Workflow Mode = Specular   (default)
    urp-metallic   URP/Lit, Workflow Mode = Metallic
    hdrp           HDRP/Lit, metallic + AO + smoothness packed into a mask map
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import zipfile

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

try:
    import cv2
    import numpy as np
    CV_ERROR = None
except ImportError as exc:  # the Rename tab still works without them
    cv2 = np = None
    CV_ERROR = str(exc)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------
# workspace
# --------------------------------------------------------------------------

DEFAULT_WORKSPACE = r"d:\Unity\Downloads"
QUEUE_DIRNAME = "Queue"
CONVERTED_DIRNAME = "Converted"

QUEUE_README = """\
Drop your downloaded texture sets in here.

One folder per material is the tidy way, but anything goes -- the scanner walks
subfolders and groups image files into sets by filename, so an unpacked
"Metal021_2K-JPG" folder or a nest of "textures/" subfolders both work. Zips are
extracted in place when "Extract .zip archives" is ticked.

Nothing in this folder is ever modified or deleted. Results go to ../%s/.
""" % CONVERTED_DIRNAME


def ensure_workspace(workspace):
    """Create <workspace>/Queue and /Converted. Returns (queue, converted, created)."""
    workspace = os.path.abspath(workspace)
    q = os.path.join(workspace, QUEUE_DIRNAME)
    c = os.path.join(workspace, CONVERTED_DIRNAME)
    created = [p for p in (workspace, q, c) if not os.path.isdir(p)]
    for path in (workspace, q, c):
        os.makedirs(path, exist_ok=True)

    readme = os.path.join(q, "_READ_ME.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write(QUEUE_README)
        except OSError:
            pass
    return q, c, created


def extract_archives(queue_dir, log=print):
    """Unpack every .zip sitting in the queue into a folder of the same name."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(queue_dir):
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            if not fn.lower().endswith(".zip"):
                continue
            src = os.path.join(dirpath, fn)
            dest = os.path.join(dirpath, os.path.splitext(fn)[0])
            if os.path.isdir(dest):
                continue
            try:
                with zipfile.ZipFile(src) as zf:
                    safe = [m for m in zf.namelist()
                            if not os.path.isabs(m) and ".." not in m.split("/")]
                    zf.extractall(dest, safe)
                log("  extracted %s" % fn)
                count += 1
            except (zipfile.BadZipFile, OSError) as exc:
                log("  !! could not extract %s (%s)" % (fn, exc))
    return count


# --------------------------------------------------------------------------
# pipelines
# --------------------------------------------------------------------------

PIPE_URP_SPEC = "urp-specular"
PIPE_URP_METAL = "urp-metallic"
PIPE_HDRP = "hdrp"

PIPELINE_LABELS = {
    PIPE_URP_SPEC: "URP/Lit  -  Specular workflow",
    PIPE_URP_METAL: "URP/Lit  -  Metallic workflow",
    PIPE_HDRP: "HDRP/Lit  -  packed Mask Map",
}
LABEL_TO_PIPELINE = {v: k for k, v in PIPELINE_LABELS.items()}

# Dielectric reflectance at normal incidence, linear. 0.04 == Unity's default.
DIELECTRIC_F0 = 0.04

SUFFIXES = {
    PIPE_URP_SPEC: {
        "basecolor": "_BaseColor",
        "specular": "_Specular",
        "normal": "_Normal",
        "height": "_Height",
        "occlusion": "_Occlusion",
        "emissive": "_Emissive",
    },
    PIPE_URP_METAL: {
        "basecolor": "_BaseColor",
        "metallicsmoothness": "_MetallicSmoothness",
        "normal": "_Normal",
        "height": "_Height",
        "occlusion": "_Occlusion",
        "emissive": "_Emissive",
    },
    PIPE_HDRP: {
        "basecolor": "_BaseColor",
        "mask": "_MaskMap",
        "normal": "_Normal",
        "height": "_Height",
        "emissive": "_Emissive",
    },
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
    out_root = os.path.abspath(out_root)
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
    for part in re.split(r"[^A-Za-z0-9]+", raw):  # "Foliage 03 (2K)" -> Foliage, 03, 2K
        if not part or NOISE_TOKENS.match(part):
            continue
        for piece in re.findall(r"\d+|[A-Za-z]+", part):  # "Metal021" -> "Metal", "021"
            if NOISE_TOKENS.match(piece):
                continue
            tokens.append(piece.upper())
    return "T_" + "_".join(tokens or ["MATERIAL"])


def normalize_name(name):
    """Sanitise a typed or derived name into T_SOMETHING."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", name or "").upper()
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "MATERIAL"
    return name if name.startswith("T_") else "T_" + name


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

def plan_outputs(pipeline, maps, name, out_dir):
    """Which files this pipeline will write for this set, logical key -> path."""
    sfx = SUFFIXES[pipeline]
    planned = {}

    def add(key):
        planned[key] = os.path.join(out_dir, name + sfx[key] + ".png")

    has_color = "basecolor" in maps
    has_surface = bool(maps.keys() & {"metallic", "roughness", "glossiness", "occlusion"})

    if has_color:
        add("basecolor")
    if pipeline == PIPE_URP_SPEC:
        if has_color:
            add("specular")
    elif pipeline == PIPE_URP_METAL:
        if has_color or has_surface:
            add("metallicsmoothness")
    else:  # hdrp packs metallic / ao / detail / smoothness into one map
        if has_color or has_surface:
            add("mask")

    if maps.keys() & {"normal_gl", "normal_dx", "normal"}:
        add("normal")
    if "height" in maps:
        add("height")
    if "occlusion" in maps and "occlusion" in sfx:
        add("occlusion")
    if "emissive" in maps:
        add("emissive")
    return planned


def stale_outputs(pipeline, name, out_dir):
    """Maps already in the output folder that this pipeline does not use.

    Converting the same set twice into two different pipelines leaves the first
    one's maps sitting there -- an unused _Specular next to a _MaskMap, say.
    They are never touched automatically, only reported.
    """
    wanted = set(SUFFIXES[pipeline].values())
    others = set()
    for key, suffixes in SUFFIXES.items():
        if key != pipeline:
            others |= set(suffixes.values())

    found = []
    for suffix in sorted(others - wanted):
        path = os.path.join(out_dir, name + suffix + ".png")
        if os.path.exists(path):
            found.append(path)
    return found


def convert(folder, maps, name, out_dir, pipeline, dry_run=False, force=False, log=print):
    """Build one material set. Returns (status, written paths by logical key)."""
    planned = plan_outputs(pipeline, maps, name, out_dir)
    notes_path = os.path.join(out_dir, name + "_ImportNotes.txt")
    outputs = list(planned.values()) + [notes_path]

    if not planned:
        log("  skip   %s  (nothing this pipeline can build)" % name)
        return "skipped", {}
    if not needs_rebuild(list(maps.values()), outputs, force):
        log("  skip   %s  (up to date)" % name)
        return "skipped", {}

    log("  build  %s" % name)
    for key in sorted(maps):
        log("           %-11s <- %s" % (key, os.path.basename(maps[key])))
    if dry_run:
        for path in planned.values():
            log("           would write %s" % os.path.basename(path))
        return "dry-run", {}

    os.makedirs(out_dir, exist_ok=True)
    written = {}

    # the colour map sets the output resolution; every other map is fitted to it
    ref_src = maps.get("basecolor") or maps.get("normal_gl") or \
        maps.get("normal_dx") or maps.get("normal")
    ref_img, _ = imread(ref_src)
    shape = ref_img.shape[:2]

    def load(key, grey=True):
        """Fitted source map, or (None, 8) when the set does not have one."""
        if key not in maps:
            return None, 8
        img, bits = imread(maps[key])
        return fit(to_grey(img) if grey else to_rgb(img), shape), bits

    def channel(key, default):
        """A single channel data map, or a constant when the set has none."""
        img, _ = load(key)
        if img is None:
            return np.full(shape, default, np.float32)
        return np.clip(img, 0.0, 1.0)

    def smoothness():
        rough, _ = load("roughness")
        if rough is not None:
            return np.clip(1.0 - rough, 0.0, 1.0)
        gloss, _ = load("glossiness")
        if gloss is not None:
            return np.clip(gloss, 0.0, 1.0)
        return np.full(shape, 0.5, np.float32)

    def opacity():
        """Alpha for the base map: an opacity map, else the albedo's own alpha."""
        img, _ = load("opacity")
        if img is not None:
            return np.clip(img, 0.0, 1.0)
        if "basecolor" in maps:
            src, _ = imread(maps["basecolor"])
            if src.ndim == 3 and src.shape[2] == 4:
                alpha = fit(src[:, :, 3], shape)
                if alpha.min() < 0.999:
                    return np.clip(alpha, 0.0, 1.0)
        return None

    albedo_lin = metal_mask = None

    # ---- base colour ------------------------------------------------------
    if "basecolor" in planned:
        albedo, _ = imread(maps["basecolor"])
        albedo_rgb = fit(to_rgb(albedo), shape)
        is_linear_src = os.path.splitext(maps["basecolor"])[1].lower() in (".exr", ".hdr")
        albedo_lin = albedo_rgb if is_linear_src else srgb_to_linear(albedo_rgb)
        metal_mask = channel("metallic", 0.0)[:, :, None]

        if pipeline == PIPE_URP_SPEC:
            # in Specular mode the metal contribution leaves the diffuse map
            base = linear_to_srgb(albedo_lin * (1.0 - metal_mask))
        elif is_linear_src:
            base = linear_to_srgb(albedo_lin)
        else:
            base = albedo_rgb  # already sRGB encoded, pass it through untouched

        alpha = opacity()
        if alpha is not None:
            base = np.concatenate([base, alpha[:, :, None]], axis=2)
        imwrite(planned["basecolor"], base, 8)
        written["basecolor"] = planned["basecolor"]

    # ---- the surface map, one per pipeline --------------------------------
    if "specular" in planned:
        specular_lin = DIELECTRIC_F0 * (1.0 - metal_mask) + albedo_lin * metal_mask
        spec = np.concatenate(
            [linear_to_srgb(specular_lin), smoothness()[:, :, None]], axis=2)
        imwrite(planned["specular"], spec, 8)
        written["specular"] = planned["specular"]

    if "metallicsmoothness" in planned:
        # URP/Lit metallic mode reads metallic from R and smoothness from A
        met = channel("metallic", 0.0)
        packed = np.concatenate([np.repeat(met[:, :, None], 3, axis=2),
                                 smoothness()[:, :, None]], axis=2)
        imwrite(planned["metallicsmoothness"], packed, 8)
        written["metallicsmoothness"] = planned["metallicsmoothness"]

    if "mask" in planned:
        # HDRP/Lit mask map: R metallic, G occlusion, B detail, A smoothness
        mask = np.stack([
            channel("metallic", 0.0),
            channel("occlusion", 1.0),
            np.zeros(shape, np.float32),
            smoothness(),
        ], axis=2)
        imwrite(planned["mask"], mask, 8)
        written["mask"] = planned["mask"]

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

    write_notes(notes_path, name, folder, maps, written, pipeline)

    leftovers = stale_outputs(pipeline, name, out_dir)
    if leftovers:
        log("           note: %d map(s) from another pipeline are still in this folder"
            % len(leftovers))
        for path in leftovers:
            log("                 %s" % os.path.basename(path))

    return "built", written


def write_notes(path, name, folder, maps, written, pipeline):
    header = {
        PIPE_URP_SPEC: "Unity URP/Lit, Workflow Mode = Specular",
        PIPE_URP_METAL: "Unity URP/Lit, Workflow Mode = Metallic",
        PIPE_HDRP: "Unity HDRP/Lit, Material Type = Standard",
    }[pipeline]

    lines = ["%s  --  %s" % (name, header),
             "source: %s" % folder,
             "",
             "Material setup:"]
    if pipeline == PIPE_URP_SPEC:
        lines += ["  Workflow Mode ......... Specular",
                  "  Smoothness Source ..... Specular Alpha"]
    elif pipeline == PIPE_URP_METAL:
        lines += ["  Workflow Mode ......... Metallic",
                  "  Smoothness Source ..... Metallic Alpha"]
    else:
        lines += ["  Material Type ......... Standard",
                  "  Mask Map .............. R metallic, G occlusion, B detail, A smoothness",
                  "  Smoothness Remapping .. leave at 0 - 1 unless you want to retune it",
                  "  Ambient Occlusion ..... comes from the mask map, no separate slot"]
    lines += ["", "Texture import settings:"]

    def note(key, *body):
        if key in written:
            lines.append("  " + os.path.basename(written[key]))
            lines.extend("      " + text for text in body)

    note("basecolor", "-> Base Map      | sRGB ON")
    note("specular",
         "-> Specular Map  | sRGB ON | Alpha Source: Input Texture Alpha",
         "-> compression must preserve alpha (BC7 / DXT5), not DXT1")
    note("metallicsmoothness",
         "-> Metallic Map  | sRGB OFF | Alpha Source: Input Texture Alpha",
         "-> compression must preserve alpha (BC7 / DXT5), not DXT1")
    note("mask",
         "-> Mask Map      | sRGB OFF | Alpha Source: Input Texture Alpha",
         "-> compression must preserve alpha (BC7), not DXT1")
    note("normal", "-> Normal Map    | Texture Type: Normal map (already OpenGL / Y+)")
    note("height", "-> Height Map    | sRGB OFF")
    note("occlusion", "-> Occlusion Map | sRGB OFF")
    note("emissive", "-> Emission Map  | sRGB ON")

    lines += ["", "Conversion applied (in linear space):"]
    if pipeline == PIPE_URP_SPEC:
        lines += ["  diffuse    = albedo * (1 - metallic)",
                  "  specular   = lerp(%s, albedo, metallic)" % DIELECTRIC_F0,
                  "  smoothness = 1 - roughness, packed into the specular map alpha"]
    elif pipeline == PIPE_URP_METAL:
        lines += ["  base       = albedo, passed through",
                  "  metallic   = metallic, or 0 when the set has none",
                  "  smoothness = 1 - roughness, packed into the metallic map alpha"]
    else:
        lines += ["  base       = albedo, passed through",
                  "  mask.r     = metallic, or 0 when the set has none",
                  "  mask.g     = occlusion, or 1 when the set has none",
                  "  mask.b     = 0, no detail map",
                  "  mask.a     = 1 - roughness"]

    lines += ["", "Source files:"]
    for key in sorted(maps):
        lines.append("  %-12s %s" % (key, os.path.basename(maps[key])))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def load_overrides(workspace, names_file=None):
    """Read the optional names.json sitting in the workspace root."""
    path = names_file or os.path.join(workspace, "names.json")
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {k.lower(): v for k, v in data.items() if not k.startswith("_")}, path
    except (OSError, ValueError, AttributeError):
        return {}, None


# --------------------------------------------------------------------------
# rename side: suffix schemes and classification of already converted files
# --------------------------------------------------------------------------

PREFIX = "T"

# Material types offered in the dropdown. The field stays editable, so
# anything not listed here can simply be typed in.
MATERIAL_TYPES = [
    "METAL", "WOOD", "CONCRETE", "STONE", "BRICK", "TILE", "PLASTER",
    "FABRIC", "LEATHER", "PLASTIC", "GLASS", "RUBBER", "PAINT",
    "GROUND", "ROCK", "SAND", "GRASS", "SNOW", "ICE", "WATER",
    "ASPHALT", "MARBLE", "CERAMIC", "PAPER", "FOLIAGE",
]

# Suffix schemes. Key = logical map, value = the suffix written to disk.
SUFFIX_SCHEMES = {
    "Unity default (_BaseColor)": {
        "basecolor": "_BaseColor",
        "specular": "_Specular",
        "metallicsmoothness": "_MetallicSmoothness",
        "mask": "_MaskMap",
        "metallic": "_Metallic",
        "normal": "_Normal",
        "height": "_Height",
        "occlusion": "_Occlusion",
        "emissive": "_Emissive",
        "roughness": "_Roughness",
        "smoothness": "_Smoothness",
        "opacity": "_Opacity",
        "notes": "_ImportNotes",
    },
    "Albedo style (_Albedo)": {
        "basecolor": "_Albedo",
        "specular": "_Specular",
        "metallicsmoothness": "_MetallicSmoothness",
        "mask": "_MaskMap",
        "metallic": "_Metallic",
        "normal": "_Normal",
        "height": "_Height",
        "occlusion": "_AO",
        "emissive": "_Emissive",
        "roughness": "_Roughness",
        "smoothness": "_Smoothness",
        "opacity": "_Opacity",
        "notes": "_ImportNotes",
    },
    "Short (_BC / _N / _S)": {
        "basecolor": "_BC",
        "specular": "_S",
        "metallicsmoothness": "_MS",
        "mask": "_MASK",
        "metallic": "_M",
        "normal": "_N",
        "height": "_H",
        "occlusion": "_AO",
        "emissive": "_E",
        "roughness": "_R",
        "smoothness": "_SM",
        "opacity": "_O",
        "notes": "_ImportNotes",
    },
}

# How an existing filename maps back to a logical map. First match wins, so the
# packed maps have to be tested before plain metallic / smoothness.
RENAME_PATTERNS = [
    ("notes", r"(importnotes|_notes)$"),
    ("mask", r"(maskmap|_mask)$"),
    ("metallicsmoothness", r"(metall?ic[_\-]?smoothness|_ms)$"),
    ("basecolor", r"(basecolor|base_color|albedo|diffuse|_diff|_col|_alb|_bc|color)$"),
    ("specular", r"(specular|_spec|_s)$"),
    ("metallic", r"(metall?ic|metalness|_metal|_mtl|_m)$"),
    ("normal", r"(normal|_nor|_nrm|_n)$"),
    ("height", r"(height|displacement|_disp|_hgt|bump|_h)$"),
    ("occlusion", r"(ambientocclusion|occlusion|_ao)$"),
    ("emissive", r"(emissi(ve|on)|_e)$"),
    ("roughness", r"(rough(ness)?|_rgh|_r)$"),
    ("smoothness", r"(smoothness|gloss(iness)?|_sm)$"),
    ("opacity", r"(opacity|alpha|transparen|_o)$"),
]


# An output file named on a line of its own inside an _ImportNotes.txt, which is
# how write_notes lists the import settings. The "Source files:" entries put a
# map key in front of the name, so they are left alone and keep recording where
# the material came from.
NOTE_REFERENCE = re.compile(r"^([ \t]*)([A-Za-z0-9_]+\.(?:png|txt))[ \t]*$",
                            re.IGNORECASE | re.MULTILINE)


def classify_output(stem):
    """Return the logical map name for a converted file stem, or None."""
    low = stem.lower()
    for key, pattern in RENAME_PATTERNS:
        if re.search(pattern, low):
            return key
    return None


def sanitize(text):
    """'metal plate!' -> 'METAL_PLATE'"""
    text = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip())
    return re.sub(r"_+", "_", text).strip("_").upper()


def split_existing(stem):
    """Split 'T_METAL_021_BaseColor' into ('T_METAL_021', 'basecolor')."""
    key = classify_output(stem)
    if not key:
        return stem, None
    for pattern in (p for k, p in RENAME_PATTERNS if k == key):
        match = re.search(pattern, stem.lower())
        if match:
            return stem[: match.start()].rstrip("_"), key
    return stem, key


def guess_from_folder(folder_name):
    """Pre-fill the type / id fields from an existing T_TYPE_ID folder name."""
    tokens = [t for t in re.split(r"[\s_\-.]+", folder_name) if t]
    if tokens and tokens[0].upper() == PREFIX:
        tokens = tokens[1:]
    if not tokens:
        return "", ""
    return tokens[0].upper(), "_".join(tokens[1:]).upper()


def open_in_explorer(path):
    """Show a folder in the OS file browser."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    except OSError as exc:
        messagebox.showerror("Cannot open folder", str(exc))


# --------------------------------------------------------------------------
# ui -- convert tab
# --------------------------------------------------------------------------

class ConvertTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app
        self.columnconfigure(1, weight=1)

        self.pipeline_label = tk.StringVar(value=PIPELINE_LABELS[PIPE_URP_SPEC])
        self.force = tk.BooleanVar(value=False)
        self.dry_run = tk.BooleanVar(value=False)
        self.unzip = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Scan the queue to get started.")

        self.sets = {}       # source folder -> {logical map: source path}
        self.names = {}      # source folder -> chosen output name
        self.rows = {}       # tree row id -> source folder
        self.editor = None   # the inline name entry, when one is open
        self.worker = None
        self.messages = queue.Queue()

        self._build()
        self.after(120, self._drain)

    # ------------------------------------------------------------- layout

    def _build(self):
        row = 0
        ttk.Label(self, text="Pipeline").grid(row=row, column=0, sticky="w", pady=2)
        box = ttk.Combobox(self, textvariable=self.pipeline_label, state="readonly",
                           values=[PIPELINE_LABELS[k] for k in
                                   (PIPE_URP_SPEC, PIPE_URP_METAL, PIPE_HDRP)])
        box.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        box.bind("<<ComboboxSelected>>", lambda e: self.refresh_rows())

        row += 1
        opts = ttk.Frame(self)
        opts.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        ttk.Checkbutton(opts, text="Rebuild even if up to date",
                        variable=self.force).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Dry run (write nothing)",
                        variable=self.dry_run).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Checkbutton(opts, text="Extract .zip archives",
                        variable=self.unzip).grid(row=0, column=2, sticky="w", padx=(16, 0))

        row += 1
        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        ttk.Button(bar, text="Scan queue", command=self.scan).grid(row=0, column=0)
        ttk.Button(bar, text="Open queue",
                   command=lambda: open_in_explorer(self.app.queue_dir)).grid(
            row=0, column=1, padx=(8, 0))
        ttk.Button(bar, text="Open converted",
                   command=lambda: open_in_explorer(self.app.converted_dir)).grid(
            row=0, column=2, padx=(8, 0))
        ttk.Button(bar, text="Reset names", command=self.reset_names).grid(
            row=0, column=3, padx=(8, 0))

        row += 1
        ttk.Label(self, text="Double click an output name to change it, or leave the "
                             "auto derived default.", foreground="#666").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))

        row += 1
        self.tree = ttk.Treeview(self, columns=("source", "maps", "name", "state"),
                                 show="headings", height=9)
        for col, text, width in (("source", "Queue folder", 200),
                                 ("maps", "Maps found", 190),
                                 ("name", "Output name", 180),
                                 ("state", "Status", 90)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.grid(row=row, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scroll.grid(row=row, column=3, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self.begin_edit)
        self.rowconfigure(row, weight=3)

        row += 1
        self.log = tk.Text(self, height=8, wrap="none", state="disabled",
                           font=("Consolas", 9))
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        log_scroll = ttk.Scrollbar(self, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=row, column=3, sticky="ns", pady=(8, 4))
        self.log.configure(yscrollcommand=log_scroll.set)
        self.rowconfigure(row, weight=2)

        row += 1
        ttk.Label(self, textvariable=self.status, foreground="#444").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(2, 6))

        row += 1
        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e")
        self.selected_button = ttk.Button(buttons, text="Convert selected",
                                          command=lambda: self.run(True), state="disabled")
        self.selected_button.grid(row=0, column=0, padx=(0, 8))
        self.all_button = ttk.Button(buttons, text="Convert all",
                                     command=lambda: self.run(False), state="disabled")
        self.all_button.grid(row=0, column=1)

    # ---------------------------------------------------------- behaviour

    @property
    def pipeline(self):
        return LABEL_TO_PIPELINE[self.pipeline_label.get()]

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def scan(self):
        if self.busy():
            return
        self.cancel_edit()
        self.clear_log()
        if CV_ERROR:
            self.write("!! opencv-python / numpy are missing (%s)" % CV_ERROR)
            self.write("   pip install opencv-python numpy")
        self.write("queue    %s" % self.app.queue_dir)
        self.write("output   %s" % self.app.converted_dir)

        if self.unzip.get():
            found = extract_archives(self.app.queue_dir, self.write)
            if found:
                self.write("extracted %d archive(s)" % found)

        self.sets = find_sets(self.app.queue_dir, self.app.converted_dir)
        self.reset_names(quiet=True)
        self.write("found %d material set(s)" % len(self.sets))

    def reset_names(self, quiet=False):
        self.cancel_edit()
        overrides, names_path = load_overrides(self.app.workspace.get())
        if names_path and not quiet:
            self.write("name overrides: %s" % names_path)
        self.names = {}
        for folder in self.sets:
            key = os.path.basename(os.path.normpath(folder)).lower()
            self.names[folder] = normalize_name(overrides.get(key) or derive_name(folder))
        self.refresh_rows()

    def refresh_rows(self):
        self.cancel_edit()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        pipeline = self.pipeline

        for folder in sorted(self.sets):
            maps = self.sets[folder]
            name = self.names.get(folder, "")
            out_dir = os.path.join(self.app.converted_dir, name)
            planned = plan_outputs(pipeline, maps, name, out_dir)
            notes = os.path.join(out_dir, name + "_ImportNotes.txt")
            if not planned:
                state = "no output"
            elif needs_rebuild(list(maps.values()), list(planned.values()) + [notes], False):
                state = "pending"
            else:
                state = "up to date"
            iid = self.tree.insert("", "end", values=(
                os.path.relpath(folder, self.app.queue_dir),
                ", ".join(sorted(maps)),
                name,
                state))
            self.rows[iid] = folder

        state = "!disabled" if self.sets else "disabled"
        self.selected_button.state([state])
        self.all_button.state([state])
        if not self.sets:
            self.status.set("Nothing in the queue yet. Drop texture folders in %s"
                            % self.app.queue_dir)
        else:
            self.status.set("%d set(s) found. Target: %s"
                            % (len(self.sets), PIPELINE_LABELS[pipeline]))

    # -------------------------------------------------- inline name editor

    def begin_edit(self, event):
        self.cancel_edit()
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#3":  # the name column
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return

        x, y, w, h = self.tree.bbox(iid, "name")
        var = tk.StringVar(value=self.tree.set(iid, "name"))
        entry = ttk.Entry(self.tree, textvariable=var)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.selection_range(0, "end")
        entry.bind("<Return>", lambda e: self.commit_edit(iid, var.get()))
        entry.bind("<FocusOut>", lambda e: self.commit_edit(iid, var.get()))
        entry.bind("<Escape>", lambda e: self.cancel_edit())
        self.editor = entry

    def commit_edit(self, iid, text):
        editor, self.editor = self.editor, None
        if editor is not None:
            editor.destroy()
        folder = self.rows.get(iid)
        if folder is None:
            return
        name = normalize_name(text)
        if name != self.names.get(folder):
            self.names[folder] = name
            self.refresh_rows()

    def cancel_edit(self):
        if self.editor is not None:
            self.editor.destroy()
            self.editor = None

    # ------------------------------------------------------------- running

    def run(self, selected_only):
        if self.busy():
            return
        self.cancel_edit()
        if CV_ERROR:
            messagebox.showerror("Missing dependency",
                                 "Converting needs opencv-python and numpy.\n\n"
                                 "pip install opencv-python numpy")
            return

        folders = [self.rows[i] for i in self.tree.selection()] if selected_only \
            else sorted(self.sets)
        if not folders:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return

        chosen = [(f, self.sets[f], self.names[f]) for f in folders]
        seen = {}
        for _, _, name in chosen:
            seen[name] = seen.get(name, 0) + 1
        duplicates = sorted(n for n, count in seen.items() if count > 1)
        if duplicates:
            messagebox.showerror("Duplicate names",
                                 "These output names are used by more than one set:\n\n"
                                 + "\n".join(duplicates))
            return

        self.selected_button.state(["disabled"])
        self.all_button.state(["disabled"])
        self.status.set("Converting %d set(s)..." % len(chosen))
        job = dict(sets=chosen, pipeline=self.pipeline, out_root=self.app.converted_dir,
                   force=self.force.get(), dry_run=self.dry_run.get())
        self.worker = threading.Thread(target=self._work, args=(job,), daemon=True)
        self.worker.start()

    def _work(self, job):
        """Runs off the UI thread; every line goes back through self.messages."""
        say = self.messages.put
        log = lambda text: say(("log", text))
        tally = {}
        log("")
        log("converting %d set(s) -> %s"
            % (len(job["sets"]), PIPELINE_LABELS[job["pipeline"]]))
        for folder, maps, name in job["sets"]:
            try:
                status, _ = convert(folder, maps, name,
                                    os.path.join(job["out_root"], name),
                                    job["pipeline"], job["dry_run"], job["force"],
                                    log=log)
            except Exception as exc:  # a bad source file must not kill the batch
                log("  !! %s failed: %s" % (name, exc))
                status = "failed"
            tally[status] = tally.get(status, 0) + 1
        log(", ".join("%d %s" % (v, k) for k, v in sorted(tally.items())))
        say(("done", tally))

    def _drain(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.write(payload)
                else:
                    self.refresh_rows()
                    self.status.set(", ".join("%d %s" % (v, k)
                                              for k, v in sorted(payload.items()))
                                    or "Nothing to do.")
                    self.app.rename_tab.refresh_sets()
        except queue.Empty:
            pass
        self.after(120, self._drain)


# --------------------------------------------------------------------------
# ui -- rename tab
# --------------------------------------------------------------------------

class RenameTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app
        self.columnconfigure(1, weight=1)

        self.material_type = tk.StringVar()
        self.unique_id = tk.StringVar()
        self.scheme = tk.StringVar(value=next(iter(SUFFIX_SCHEMES)))
        self.rename_folder = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Select a material set.")
        self.last_operation = None  # list of (new_path, old_path) for undo
        self.last_notes = {}        # path -> notes text as it was before the rename

        self._build()

    # ------------------------------------------------------------- layout

    def _build(self):
        row = 0
        ttk.Label(self, text="Material set").grid(row=row, column=0, sticky="w", pady=2)
        picker = ttk.Frame(self)
        picker.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        picker.columnconfigure(0, weight=1)
        self.set_box = ttk.Combobox(picker, state="readonly")
        self.set_box.grid(row=0, column=0, sticky="ew")
        self.set_box.bind("<<ComboboxSelected>>", self.on_set_selected)
        ttk.Button(picker, text="Refresh", width=9,
                   command=self.refresh_sets).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(picker, text="Open", width=9,
                   command=lambda: open_in_explorer(
                       self.current_folder() or self.root_dir)).grid(
            row=0, column=2, padx=(6, 0))

        row += 1
        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=8)

        row += 1
        ttk.Label(self, text="Prefix").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Label(self, text=PREFIX + "_   (added automatically)",
                  foreground="#666").grid(row=row, column=1, sticky="w", pady=2)

        row += 1
        ttk.Label(self, text="Material type").grid(row=row, column=0, sticky="w", pady=2)
        type_box = ttk.Combobox(self, textvariable=self.material_type, values=MATERIAL_TYPES)
        type_box.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        type_box.bind("<KeyRelease>", lambda e: self.update_preview())
        type_box.bind("<<ComboboxSelected>>", lambda e: self.update_preview())

        row += 1
        ttk.Label(self, text="Unique ID / variant").grid(row=row, column=0, sticky="w", pady=2)
        id_entry = ttk.Entry(self, textvariable=self.unique_id)
        id_entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        id_entry.bind("<KeyRelease>", lambda e: self.update_preview())

        row += 1
        options = ttk.Frame(self)
        options.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="Suffix scheme").grid(row=0, column=0, sticky="w")
        scheme_box = ttk.Combobox(options, textvariable=self.scheme, state="readonly",
                                  values=list(SUFFIX_SCHEMES))
        scheme_box.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        scheme_box.bind("<<ComboboxSelected>>", lambda e: self.update_preview())
        ttk.Checkbutton(options, text="Rename the containing folder too",
                        variable=self.rename_folder,
                        command=self.update_preview).grid(row=1, column=0, columnspan=2,
                                                          sticky="w", pady=(6, 0))

        row += 1
        self.preview = ttk.Treeview(self, columns=("old", "new"), show="headings", height=10)
        self.preview.heading("old", text="Current name")
        self.preview.heading("new", text="New name")
        self.preview.column("old", width=280, anchor="w")
        self.preview.column("new", width=280, anchor="w")
        self.preview.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.preview.yview)
        scroll.grid(row=row, column=3, sticky="ns", pady=(8, 4))
        self.preview.configure(yscrollcommand=scroll.set)
        self.rowconfigure(row, weight=1)

        row += 1
        ttk.Label(self, textvariable=self.status, foreground="#444").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(2, 6))

        row += 1
        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e")
        self.undo_button = ttk.Button(buttons, text="Undo last rename", state="disabled",
                                      command=self.undo)
        self.undo_button.grid(row=0, column=0, padx=(0, 8))
        self.apply_button = ttk.Button(buttons, text="Apply rename", state="disabled",
                                       command=self.apply)
        self.apply_button.grid(row=0, column=1)

    # ---------------------------------------------------------- behaviour

    @property
    def root_dir(self):
        return self.app.converted_dir

    def current_folder(self):
        name = self.set_box.get()
        return os.path.join(self.root_dir, name) if name else None

    def refresh_sets(self):
        root = self.root_dir
        if not os.path.isdir(root):
            self.set_box["values"] = []
            self.set_box.set("")
            self.clear_preview("Folder not found: %s" % root)
            return

        folders = sorted(d for d in os.listdir(root)
                         if os.path.isdir(os.path.join(root, d)))
        self.set_box["values"] = folders
        if not folders:
            self.set_box.set("")
            self.clear_preview("Nothing converted yet -- use the Convert tab first.")
            return

        if self.set_box.get() not in folders:
            self.set_box.set(folders[0])
        self.on_set_selected()

    def on_set_selected(self, _event=None):
        guessed_type, guessed_id = guess_from_folder(self.set_box.get())
        self.material_type.set(guessed_type)
        self.unique_id.set(guessed_id)
        self.update_preview()

    def clear_preview(self, message):
        self.preview.delete(*self.preview.get_children())
        self.status.set(message)
        self.apply_button.state(["disabled"])

    def build_plan(self):
        """Return (list of (old_path, new_path), warning) for the current inputs."""
        folder = self.current_folder()
        if not folder or not os.path.isdir(folder):
            return [], "Select a material set."

        mat_type = sanitize(self.material_type.get())
        mat_id = sanitize(self.unique_id.get())
        if not mat_type:
            return [], "Enter a material type."
        if not mat_id:
            return [], "Enter a unique ID or variant."

        base = "%s_%s_%s" % (PREFIX, mat_type, mat_id)
        suffixes = SUFFIX_SCHEMES[self.scheme.get()]

        plan, unknown = [], 0
        for filename in sorted(os.listdir(folder)):
            path = os.path.join(folder, filename)
            if not os.path.isfile(path):
                continue
            stem, ext = os.path.splitext(filename)
            _, key = split_existing(stem)
            if key:
                new_name = base + suffixes[key] + ext
            else:
                new_name = base + ext  # no recognised map suffix, keep it bare
                unknown += 1
            plan.append((path, os.path.join(folder, new_name)))

        if not plan:
            return [], "That folder has no files."

        new_names = [os.path.basename(p) for _, p in plan]
        if len(set(new_names)) != len(new_names):
            return plan, "Name collision: two files would end up identical."

        warning = ""
        if unknown:
            warning = "%d file(s) had no recognised map suffix." % unknown
        return plan, warning

    def update_preview(self):
        plan, warning = self.build_plan()
        self.preview.delete(*self.preview.get_children())

        for old, new in plan:
            old_name, new_name = os.path.basename(old), os.path.basename(new)
            tag = "same" if old_name == new_name else ""
            self.preview.insert("", "end", values=(old_name, new_name), tags=(tag,))
        self.preview.tag_configure("same", foreground="#999")

        blocking = warning.startswith(("Select", "Enter", "Name collision", "That folder"))
        if plan and not blocking:
            folder = self.current_folder()
            target = os.path.basename(os.path.dirname(plan[0][1]))
            if self.rename_folder.get():
                target = self.folder_target()
                if os.path.exists(os.path.join(self.root_dir, target)) and \
                        os.path.normcase(target) != os.path.normcase(os.path.basename(folder)):
                    self.status.set("A folder named %s already exists." % target)
                    self.apply_button.state(["disabled"])
                    return
            self.status.set(("%d file(s) will be renamed into %s. %s" %
                             (len(plan), target, warning)).strip())
            self.apply_button.state(["!disabled"])
        else:
            self.status.set(warning)
            self.apply_button.state(["disabled"])

    def folder_target(self):
        return "%s_%s_%s" % (PREFIX, sanitize(self.material_type.get()),
                             sanitize(self.unique_id.get()))

    def apply(self):
        plan, warning = self.build_plan()
        if not plan or warning.startswith("Name collision"):
            messagebox.showerror("Cannot rename", warning or "Nothing to do.")
            return

        changes = [(o, n) for o, n in plan if os.path.normcase(o) != os.path.normcase(n)]
        folder = self.current_folder()
        new_folder = os.path.join(self.root_dir, self.folder_target())
        folder_moves = (self.rename_folder.get() and
                        os.path.normcase(folder) != os.path.normcase(new_folder))

        if not changes and not folder_moves:
            messagebox.showinfo("Nothing to do", "Every file already has that name.")
            return

        summary = "Rename %d file(s)" % len(changes)
        if folder_moves:
            summary += "\nand the folder to %s" % os.path.basename(new_folder)
        if not messagebox.askokcancel("Confirm rename", summary + "?"):
            return

        notes = self.read_notes(folder)  # taken before anything moves, for undo
        done = []
        try:
            for old, new in changes:
                if os.path.exists(new):
                    raise OSError("target already exists: " + os.path.basename(new))
                os.rename(old, new)
                done.append((new, old))
            self.rewrite_notes(folder, self.folder_target(),
                               SUFFIX_SCHEMES[self.scheme.get()])
            if folder_moves:
                if os.path.exists(new_folder):
                    raise OSError("folder already exists: " + os.path.basename(new_folder))
                os.rename(folder, new_folder)
                done.append((new_folder, folder))
        except OSError as exc:
            for new, old in reversed(done):
                try:
                    os.rename(new, old)
                except OSError:
                    pass
            self.restore_notes(notes)
            messagebox.showerror("Rename failed", "%s\n\nAll changes were rolled back." % exc)
            return

        self.last_operation = done
        self.last_notes = notes
        self.undo_button.state(["!disabled"])
        self.refresh_sets()
        if folder_moves:
            self.set_box.set(os.path.basename(new_folder))
            self.on_set_selected()
        self.status.set("Renamed %d file(s)." % len(changes))

    def read_notes(self, folder):
        """Snapshot the notes text so undo can put it back verbatim."""
        snapshot = {}
        for filename in sorted(os.listdir(folder)):
            if not filename.lower().endswith(".txt"):
                continue
            path = os.path.join(folder, filename)
            try:
                with open(path, encoding="utf-8") as fh:
                    snapshot[path] = fh.read()
            except OSError:
                pass
        return snapshot

    def rewrite_notes(self, folder, base, suffixes):
        """Re-point the _ImportNotes text at the names now on disk.

        Every reference is rebuilt from its map suffix rather than swapped for
        the name it used to have, so notes that have drifted out of step with
        their folder still come out right.
        """
        for filename in sorted(os.listdir(folder)):
            if not filename.lower().endswith(".txt"):
                continue
            path = os.path.join(folder, filename)

            def swap(match):
                indent, stem_ext = match.group(1), match.group(2)
                stem, ext = os.path.splitext(stem_ext)
                _, key = split_existing(stem)
                return indent + (base + suffixes[key] + ext if key else stem_ext)

            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                text = NOTE_REFERENCE.sub(swap, text)
                # the material name opens the first line: "T_METAL_021  --  Unity ..."
                text = re.sub(r"^[A-Za-z0-9_]+", base, text, count=1)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass

    def restore_notes(self, snapshot):
        for path, text in snapshot.items():
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass

    def undo(self):
        if not self.last_operation:
            return
        failures = 0
        for new, old in reversed(self.last_operation):
            try:
                os.rename(new, old)
            except OSError:
                failures += 1
        self.restore_notes(self.last_notes)
        self.last_operation = None
        self.last_notes = {}
        self.undo_button.state(["disabled"])
        self.refresh_sets()
        self.status.set("Undone." if not failures else
                        "Undone with %d failure(s)." % failures)


# --------------------------------------------------------------------------
# ui -- shell
# --------------------------------------------------------------------------

class App(ttk.Frame):
    def __init__(self, master, workspace):
        super().__init__(master, padding=(10, 10, 10, 0))
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        self.workspace = tk.StringVar(value=os.path.abspath(workspace))
        self.queue_dir, self.converted_dir, created = ensure_workspace(self.workspace.get())
        self.paths = tk.StringVar()

        ttk.Label(self, text="Workspace").grid(row=0, column=0, sticky="w")
        bar = ttk.Frame(self)
        bar.grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)
        bar.columnconfigure(0, weight=1)
        ttk.Entry(bar, textvariable=self.workspace).grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Browse", width=9, command=self.browse).grid(
            row=0, column=1, padx=(6, 0))
        ttk.Button(bar, text="Use", width=9, command=self.apply_workspace).grid(
            row=0, column=2, padx=(6, 0))

        ttk.Label(self, textvariable=self.paths, foreground="#666").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))

        book = ttk.Notebook(self)
        book.grid(row=2, column=0, columnspan=3, sticky="nsew")
        self.convert_tab = ConvertTab(book, self)
        self.rename_tab = RenameTab(book, self)
        book.add(self.convert_tab, text="  Convert  ")
        book.add(self.rename_tab, text="  Rename  ")

        self.update_paths(created)
        self.convert_tab.scan()
        self.rename_tab.refresh_sets()

    def update_paths(self, created=()):
        note = "  (created)" if created else ""
        self.paths.set("queue  %s%s     converted  %s%s"
                       % (self.queue_dir, note, self.converted_dir, note))

    def browse(self):
        chosen = filedialog.askdirectory(initialdir=self.workspace.get() or os.getcwd(),
                                         title="Select the workspace folder")
        if chosen:
            self.workspace.set(os.path.normpath(chosen))
            self.apply_workspace()

    def apply_workspace(self):
        try:
            self.queue_dir, self.converted_dir, created = \
                ensure_workspace(self.workspace.get())
        except OSError as exc:
            messagebox.showerror("Cannot use that folder", str(exc))
            return
        self.update_paths(created)
        self.convert_tab.scan()
        self.rename_tab.refresh_sets()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def run_cli(args):
    if CV_ERROR:
        sys.exit("converting needs opencv-python and numpy (%s)" % CV_ERROR)

    queue_dir, converted_dir, created = ensure_workspace(args.workspace)
    if created:
        print("created:\n  " + "\n  ".join(created))
    print("queue    %s\noutput   %s\n" % (queue_dir, converted_dir))

    if args.extract:
        extract_archives(queue_dir)

    overrides, names_path = load_overrides(args.workspace, args.names)
    if names_path:
        print("name overrides: %s" % names_path)

    sets = find_sets(queue_dir, converted_dir)
    if args.only:
        sets = {k: v for k, v in sets.items()
                if args.only.lower() in os.path.basename(os.path.normpath(k)).lower()}
    if not sets:
        sys.exit("no texture sets found in " + queue_dir)
    if args.name and len(sets) > 1:
        sys.exit("--name needs exactly one set, found %d (narrow it with --only)" % len(sets))

    tally = {}
    for folder in sorted(sets):
        key = os.path.basename(os.path.normpath(folder)).lower()
        name = normalize_name(args.name or overrides.get(key) or derive_name(folder))
        status, _ = convert(folder, sets[folder], name,
                            os.path.join(converted_dir, name), args.pipeline,
                            args.dry_run, args.force)
        tally[status] = tally.get(status, 0) + 1

    print("\n" + ", ".join("%d %s" % (v, k) for k, v in sorted(tally.items())))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                    help="folder holding Queue/ and Converted/ (default: %(default)s)")
    ap.add_argument("--cli", action="store_true",
                    help="convert the queue without opening the UI")
    ap.add_argument("--pipeline", default=PIPE_URP_SPEC,
                    choices=[PIPE_URP_SPEC, PIPE_URP_METAL, PIPE_HDRP],
                    help="target pipeline for --cli (default: %(default)s)")
    ap.add_argument("--name", default=None,
                    help="force the material name for a single set, e.g. T_METAL_PLATE")
    ap.add_argument("--only", default=None,
                    help="only process queue folders whose name contains this")
    ap.add_argument("--names", default=None,
                    help="json file mapping queue folder name -> T_ name")
    ap.add_argument("--extract", action="store_true",
                    help="unpack .zip archives in the queue first")
    ap.add_argument("--force", action="store_true",
                    help="reconvert even if outputs are up to date")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    if args.cli:
        return run_cli(args)

    root = tk.Tk()
    root.title("Unity Texture Tools")
    root.minsize(860, 760)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, args.workspace)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
