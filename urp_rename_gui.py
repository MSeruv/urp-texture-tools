#!/usr/bin/env python3
"""
Rename Unity URP texture sets to the T_MATERIALTYPE_ID convention.

A small Tkinter front end over the Unity_URP output folder. Pick a material
set, type the material type and the unique id / variant, and every file in
the set is renamed to:

    T_<TYPE>_<ID><MapSuffix>.png

The "T_" prefix is added automatically and is not editable. Map suffixes
(_BaseColor / _Albedo, _Normal, _Specular, ...) are detected on the existing
files and re-applied, so a set never loses track of which map is which.

    python urp_rename_gui.py
    python urp_rename_gui.py --root "d:\\Unity\\Downloads\\Unity_URP"
"""

import argparse
import os
import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

DEFAULT_ROOT = r"d:\Unity\Downloads\Unity_URP"
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
        "metallic": "_Metallic",
        "normal": "_Normal",
        "height": "_Height",
        "occlusion": "_Occlusion",
        "emissive": "_Emissive",
        "roughness": "_Roughness",
        "smoothness": "_Smoothness",
        "opacity": "_Opacity",
        "mask": "_MaskMap",
        "notes": "_ImportNotes",
    },
    "Albedo style (_Albedo)": {
        "basecolor": "_Albedo",
        "specular": "_Specular",
        "metallic": "_Metallic",
        "normal": "_Normal",
        "height": "_Height",
        "occlusion": "_AO",
        "emissive": "_Emissive",
        "roughness": "_Roughness",
        "smoothness": "_Smoothness",
        "opacity": "_Opacity",
        "mask": "_MaskMap",
        "notes": "_ImportNotes",
    },
    "Short (_BC / _N / _S)": {
        "basecolor": "_BC",
        "specular": "_S",
        "metallic": "_M",
        "normal": "_N",
        "height": "_H",
        "occlusion": "_AO",
        "emissive": "_E",
        "roughness": "_R",
        "smoothness": "_SM",
        "opacity": "_O",
        "mask": "_MASK",
        "notes": "_ImportNotes",
    },
}

# How an existing filename is mapped back to a logical map. First match wins.
MAP_PATTERNS = [
    ("notes", r"(importnotes|_notes)$"),
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
    ("mask", r"(maskmap|_mask)$"),
]


def classify(stem):
    """Return the logical map name for a file stem, or None if unrecognised."""
    low = stem.lower()
    for key, pattern in MAP_PATTERNS:
        if re.search(pattern, low):
            return key
    return None


def sanitize(text):
    """'metal plate!' -> 'METAL_PLATE'"""
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    return re.sub(r"_+", "_", text).strip("_").upper()


def split_existing(stem):
    """Split 'T_METAL_021_BaseColor' into ('T_METAL_021', 'basecolor')."""
    key = classify(stem)
    if not key:
        return stem, None
    for pattern in (p for k, p in MAP_PATTERNS if k == key):
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


class RenameApp(ttk.Frame):
    def __init__(self, master, root_dir):
        super().__init__(master, padding=10)
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)

        self.root_dir = tk.StringVar(value=root_dir)
        self.material_type = tk.StringVar()
        self.unique_id = tk.StringVar()
        self.scheme = tk.StringVar(value=next(iter(SUFFIX_SCHEMES)))
        self.rename_folder = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Select a material set.")
        self.last_operation = None  # list of (new_path, old_path) for undo

        self._build_widgets()
        self.refresh_sets()

    # ---------------------------------------------------------------- layout

    def _build_widgets(self):
        row = 0
        ttk.Label(self, text="Unity_URP folder").grid(row=row, column=0, sticky="w")
        folder_row = ttk.Frame(self)
        folder_row.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.root_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="Browse", width=9,
                   command=self.browse).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(folder_row, text="Refresh", width=9,
                   command=self.refresh_sets).grid(row=0, column=2, padx=(6, 0))

        row += 1
        ttk.Label(self, text="Material set").grid(row=row, column=0, sticky="w", pady=(8, 2))
        self.set_box = ttk.Combobox(self, state="readonly")
        self.set_box.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(8, 2))
        self.set_box.bind("<<ComboboxSelected>>", self.on_set_selected)

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

    # ------------------------------------------------------------- behaviour

    def browse(self):
        chosen = filedialog.askdirectory(initialdir=self.root_dir.get() or os.getcwd(),
                                         title="Select the Unity_URP folder")
        if chosen:
            self.root_dir.set(os.path.normpath(chosen))
            self.refresh_sets()

    def current_folder(self):
        name = self.set_box.get()
        return os.path.join(self.root_dir.get(), name) if name else None

    def refresh_sets(self):
        root = self.root_dir.get()
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
            self.clear_preview("No material sets in %s" % root)
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
                if os.path.exists(os.path.join(self.root_dir.get(), target)) and \
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
        new_folder = os.path.join(self.root_dir.get(), self.folder_target())
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

        done = []
        try:
            for old, new in changes:
                if os.path.exists(new):
                    raise OSError("target already exists: " + os.path.basename(new))
                os.rename(old, new)
                done.append((new, old))
            self.rewrite_notes(folder, plan)
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
            messagebox.showerror("Rename failed", "%s\n\nAll changes were rolled back." % exc)
            return

        self.last_operation = done
        self.undo_button.state(["!disabled"])
        self.refresh_sets()
        if folder_moves:
            self.set_box.set(os.path.basename(new_folder))
            self.on_set_selected()
        self.status.set("Renamed %d file(s)." % len(changes))

    def rewrite_notes(self, folder, plan):
        """Point the _ImportNotes text at the new filenames."""
        # longest first, so "..._BaseColor.png" is replaced before "..._Base"
        swaps = sorted(((os.path.basename(o), os.path.basename(n)) for o, n in plan),
                       key=lambda pair: len(pair[0]), reverse=True)
        base_swaps = {(os.path.splitext(o)[0], os.path.splitext(n)[0]) for o, n in swaps}

        for filename in os.listdir(folder):
            if not filename.lower().endswith(".txt"):
                continue
            path = os.path.join(folder, filename)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for old_name, new_name in swaps:
                    text = text.replace(old_name, new_name)
                for old_stem, new_stem in sorted(base_swaps, key=lambda p: -len(p[0])):
                    text = text.replace(old_stem, new_stem)
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
        self.last_operation = None
        self.undo_button.state(["disabled"])
        self.refresh_sets()
        self.status.set("Undone." if not failures else
                        "Undone with %d failure(s)." % failures)


def main():
    ap = argparse.ArgumentParser(description="Rename Unity URP texture sets.")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="Unity_URP folder (default: %(default)s)")
    args = ap.parse_args()

    root = tk.Tk()
    root.title("Unity URP Texture Renamer")
    root.minsize(720, 560)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    RenameApp(root, os.path.abspath(args.root))
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
